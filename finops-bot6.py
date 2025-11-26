import streamlit as st
import os
from datetime import datetime
from mcp import stdio_client, StdioServerParameters
from strands import Agent, Interrupt
from strands.tools.mcp import MCPClient
from strands.hooks import HookProvider

# Tool Call Limit Hook
class ToolCallLimitHook(HookProvider):
    def __init__(self, soft_limit=5):
        self.soft_limit = soft_limit
        self.tool_call_count = 0
        
    def on_tool_execution_end(self, event):
        self.tool_call_count += 1
        
        if self.tool_call_count == self.soft_limit:
            raise Interrupt(
                message=f"이미 {self.soft_limit}번의 쿼리를 실행했습니다.",
                data={
                    "tool_calls": self.tool_call_count,
                    "partial_summary_prompt": "지금까지 수집한 정보를 바탕으로 현재까지 알 수 있는 내용을 간단히 요약해주세요."
                }
            )
    
    def on_agent_initialized(self, event):
        self.tool_call_count = 0

# Real-time Query Log Hook
class RealTimeQueryLogHook(HookProvider):
    def __init__(self, status_container):
        self.container = status_container
        self.queries = []
        self.query_count = 0
    
    def on_tool_execution_start(self, event):
        # Redshift 쿼리 툴만 기록
        if 'execute_query' in event.tool_name:
            self.query_count += 1
            sql = event.tool_input.get("sql", "")
            
            self.queries.append({
                "sql": sql, 
                "status": "실행 중",
                "timestamp": datetime.now().strftime("%H:%M:%S")
            })
            
            # 실시간 업데이트
            with self.container:
                st.write(f"**🔄 쿼리 {self.query_count} 실행 중... ({self.queries[-1]['timestamp']})**")
                st.code(sql, language="sql")
    
    def on_tool_execution_end(self, event):
        if 'execute_query' in event.tool_name and self.queries:
            self.queries[-1]["status"] = "완료 ✅"

# 1. 페이지 설정
st.set_page_config(page_title="Redshift Query Chatbot", page_icon="💰", layout="wide")
st.title("💰 AWS Cost Analysis Chatbot")

# 2. Redshift 클라이언트 초기화
@st.cache_resource
def init_redshift_client():
    """Redshift MCP 클라이언트 초기화"""
    client = MCPClient(lambda: stdio_client(
        StdioServerParameters(
            command="uvx",
            args=["awslabs.redshift-mcp-server@latest"],
            env={
                "AWS_DEFAULT_REGION": "us-east-1",
                "FASTMCP_LOG_LEVEL": "ERROR"
            }
        )
    ))
    client.start()
    return client

# 3. Agent 생성 함수
def create_agent(mcp_client, with_hook=True, query_log_hook=None):
    tools = mcp_client.list_tools_sync()
    
    system_prompt = f"""You are an AWS cost analysis assistant with access to Redshift.

🎯 GUIDELINE:
- Be efficient and combine conditions in your SQL.
- Analyze data thoroughly but efficiently.

📋 RULES:
1. **Query Efficiency**:
   - Write ONE comprehensive query instead of multiple simple queries
   - Always include date filters (line_item_usage_start_date)
   - Combine conditions in WHERE clause
   - Use subqueries/CTEs for complex analysis

Available resources:
- Cluster: redshift
- Database: cur_database
- Schema: cur
- Table: cost_and_usage_report

Common columns:
- line_item_usage_account_id: AWS account ID
- line_item_usage_start_date: Usage date (ALWAYS USE FOR FILTERING!)
- line_item_product_code: AWS service (EC2, S3, etc)
- line_item_unblended_cost: Cost amount
- line_item_usage_type: Usage type

Example efficient query:
SELECT 
  line_item_usage_account_id,
  line_item_product_code,
  SUM(line_item_unblended_cost) as total_cost
FROM cost_and_usage_report
WHERE line_item_usage_start_date >= '2025-09-01'
GROUP BY line_item_usage_account_id, line_item_product_code
ORDER BY total_cost DESC
LIMIT 10;
"""
    
    hooks = []
    if with_hook:
        hooks.append(ToolCallLimitHook(soft_limit=5))
    if query_log_hook:
        hooks.append(query_log_hook)
    
    return Agent(tools=tools, system_prompt=system_prompt, hooks=hooks)

# 4. 유틸리티 함수
def get_conversation_context(messages, max_pairs=3):
    if len(messages) <= 1:
        return ""
    max_messages = max_pairs * 2
    recent_messages = messages[-(max_messages + 1):-1]
    if not recent_messages:
        return ""
    context_parts = []
    for msg in recent_messages:
        role = "사용자" if msg["role"] == "user" else "AI"
        context_parts.append(f"{role}: {msg['content']}")
    return "\n\n".join(context_parts)

def handle_example_click(message_text):
    st.session_state.messages.append({"role": "user", "content": message_text})

# 5. 세션 상태 초기화
if "messages" not in st.session_state:
    st.session_state.messages = []

if "redshift_client" not in st.session_state:
    with st.spinner("Redshift 연결 중..."):
        try:
            st.session_state.redshift_client = init_redshift_client()
            st.success("✅ Redshift 연결 완료!")
        except Exception as e:
            st.error(f"❌ 연결 실패: {str(e)}")
            st.stop()

if "interrupt_state" not in st.session_state:
    st.session_state.interrupt_state = None

if "last_query_log" not in st.session_state:
    st.session_state.last_query_log = None

# 6. 사이드바 구성
with st.sidebar:
    st.header("💡 예시 질문")
    
    examples = [
        {
            "short": "RI/SP 현황 (최근 3개월)",
            "full": "삼성클라우드(775638497521), 삼성페이(622803788537), 삼성헬스(657197638512) 서비스의 최근 3개월 EC2 인스턴스를 Reserved Instance와 Savings Plan 상황 알려줘"
        },
        {
            "short": "비용 절감 플랜 (최근 3개월)",
            "full": "삼성클라우드(775638497521) 비용 절감 플랜과 연간 절감 가능금액을 최근 3개월 데이터기반으로 알려줘"
        },
        {
            "short": "사용자당 비용 분석 (최근 3개월)",
            "full": "빅스비(642977738847) 계정의 사용자당 월 AWS 비용 최근 3개월 계산하고, 리소스 사용 패턴과 비효율적인 부분 찾아줘"
        },
        {
            "short": "월별 비용 급증 분석",
            "full": "2025년 9월 대비 10월 비용이 20% 이상 증가한 계정과 서비스 찾아서 원인 분석해줘"
        },
        {
            "short": "3개월 추이 & 이상 패턴",
            "full": "갤럭시스토어(821125494434) 계정의 S3, CloudFront, Lambda 비용을 지난 3개월 추이로 보여주고 이상 패턴 있으면 알려줘"
        }
    ]
    
    for i, example in enumerate(examples):
        st.button(
            example["short"], 
            key=f"example_{i}", 
            use_container_width=True,
            help=example["full"],
            on_click=handle_example_click,
            args=(example["full"],)
        )
    
    st.divider()
    
    st.subheader("⚙️ 설정")
    context_pairs = st.slider("대화 컨텍스트 유지 개수", 0, 10, 3)
    st.session_state.context_pairs = context_pairs
    
    st.divider()
    
    if st.button("🗑️ 대화 초기화", use_container_width=True):
        st.session_state.messages = []
        st.session_state.interrupt_state = None
        st.session_state.last_query_log = None
        st.rerun()

# 7. 메인 채팅 영역
st.divider()

# 메시지 히스토리 출력
for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        
        # 해당 메시지의 쿼리 로그가 있으면 표시
        if msg["role"] == "assistant" and msg.get("query_log"):
            with st.expander(f"📊 실행된 쿼리 보기 ({len(msg['query_log'])}개)"):
                for j, q in enumerate(msg["query_log"]):
                    st.write(f"**쿼리 {j+1}** - {q['status']} ({q['timestamp']})")
                    st.code(q["sql"], language="sql")

# Interrupt 상태 처리
if st.session_state.interrupt_state:
    interrupt_data = st.session_state.interrupt_state
    
    st.info("**📊 중간 결과 (5번 쿼리 완료)**")
    st.markdown(interrupt_data["partial_summary"])
    
    st.warning(f"⚠️ {interrupt_data['message']} 계속 진행하시겠습니까?")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("✅ 계속 분석", use_container_width=True):
            with st.spinner("분석 계속 중..."):
                try:
                    result_obj = interrupt_data["agent"].resume()
                    response_text = getattr(result_obj, 'text', str(result_obj))
                    st.markdown(response_text)
                    
                    # 쿼리 로그 포함
                    msg_with_log = {
                        "role": "assistant", 
                        "content": response_text,
                        "query_log": interrupt_data.get("query_log", [])
                    }
                    st.session_state.messages.append(msg_with_log)
                    st.session_state.interrupt_state = None
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 오류 발생: {str(e)}")
                    st.session_state.interrupt_state = None
    
    with col2:
        if st.button("❌ 여기서 마무리", use_container_width=True):
            msg_with_log = {
                "role": "assistant", 
                "content": interrupt_data["partial_summary"],
                "query_log": interrupt_data.get("query_log", [])
            }
            st.session_state.messages.append(msg_with_log)
            st.session_state.interrupt_state = None
            st.success("✅ 중간 결과로 마무리했습니다.")
            st.rerun()

# 사용자 입력 처리
if prompt := st.chat_input("AWS 비용에 대해 질문하세요..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.rerun()

# 8. AI 응답 로직
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user" and not st.session_state.interrupt_state:
    with st.chat_message("assistant"):
        # 실시간 쿼리 로그를 위한 status 컨테이너
        status_container = st.status("🔍 분석 중...", expanded=True)
        
        try:
            # 컨텍스트 구성
            max_pairs = st.session_state.get("context_pairs", 3)
            conversation_context = get_conversation_context(st.session_state.messages, max_pairs=max_pairs)
            current_question = st.session_state.messages[-1]["content"]
            
            if conversation_context and max_pairs > 0:
                full_prompt = f"""[이전 대화]\n{conversation_context}\n\n[현재 질문]\n{current_question}\n\n위 대화 맥락을 고려하여 현재 질문에 답변해주세요."""
            else:
                full_prompt = current_question
            
            # Query Log Hook 생성
            query_log_hook = RealTimeQueryLogHook(status_container)
            
            # Agent 생성 및 실행
            agent = create_agent(st.session_state.redshift_client, query_log_hook=query_log_hook)
            result_obj = agent(full_prompt)
            
            # Status 완료 표시
            status_container.update(label="✅ 분석 완료!", state="complete")
            
            # 결과 출력
            response_text = getattr(result_obj, 'text', str(result_obj))
            st.markdown(response_text)
            
            # 쿼리 로그 포함하여 메시지 저장
            msg_with_log = {
                "role": "assistant", 
                "content": response_text,
                "query_log": query_log_hook.queries
            }
            st.session_state.messages.append(msg_with_log)
            
            # 전체 쿼리 요약 표시
            if query_log_hook.queries:
                with st.expander(f"📊 실행된 전체 쿼리 보기 ({len(query_log_hook.queries)}개)"):
                    for i, q in enumerate(query_log_hook.queries):
                        st.write(f"**쿼리 {i+1}** - {q['status']} ({q['timestamp']})")
                        st.code(q["sql"], language="sql")
            
        except Interrupt as interrupt:
            # Status 업데이트
            status_container.update(label="⚠️ 중간 확인 필요", state="error")
            
            # 중간 요약 생성
            summary_prompt = interrupt.data.get("partial_summary_prompt")
            
            # 임시 Agent로 중간 요약 생성 (Hook 없이)
            temp_agent = create_agent(st.session_state.redshift_client, with_hook=False)
            temp_agent.messages = agent.messages.copy()
            partial_result = temp_agent(summary_prompt)
            partial_summary = getattr(partial_result, 'text', str(partial_result))
            
            # Interrupt 상태 저장 (쿼리 로그 포함)
            st.session_state.interrupt_state = {
                "message": interrupt.message,
                "partial_summary": partial_summary,
                "agent": agent,
                "query_log": query_log_hook.queries
            }
            st.rerun()
            
        except Exception as e:
            status_container.update(label="❌ 오류 발생", state="error")
            error_msg = f"❌ 오류 발생: {str(e)}"
            st.error(error_msg)
            st.session_state.messages.append({"role": "assistant", "content": error_msg})
