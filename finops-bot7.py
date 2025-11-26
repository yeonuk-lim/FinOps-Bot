import streamlit as st
import os
import json
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
        if 'execute_query' in event.tool_name:
            self.query_count += 1
            sql = event.tool_input.get("sql", "")
            
            self.queries.append({
                "sql": sql, 
                "status": "실행 중",
                "timestamp": datetime.now().strftime("%H:%M:%S")
            })
            
            with self.container:
                st.write(f"**🔄 쿼리 {self.query_count} 실행 중... ({self.queries[-1]['timestamp']})**")
                st.code(sql, language="sql")
    
    def on_tool_execution_end(self, event):
        if 'execute_query' in event.tool_name and self.queries:
            self.queries[-1]["status"] = "완료 ✅"

# 1. 페이지 설정
st.set_page_config(page_title="Redshift Query Chatbot", page_icon="💰", layout="wide")
st.title("💰 AWS Cost Analysis Chatbot")

# 2. 비용 계산 로직 로드
@st.cache_data
def load_cost_rules():
    """비용 계산 로직 JSON 파일 로드"""
    try:
        with open("cost_calculation_rules.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        st.error("❌ cost_calculation_rules.json 파일을 찾을 수 없습니다.")
        return {}

# 3. Redshift 클라이언트 초기화
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

# 4. Agent 생성 함수
def create_agent(mcp_client, cost_rules, with_hook=True, query_log_hook=None):
    tools = mcp_client.list_tools_sync()
    
    # 비용 계산 로직을 System Prompt에 포함
    cost_rules_text = json.dumps(cost_rules, indent=2, ensure_ascii=False)
    
    system_prompt = f"""You are an AWS cost analysis assistant with access to Redshift.

🎯 ANALYSIS GOAL:
{cost_rules.get('analysis_goal', '')}

📋 CRITICAL: AWS CUR COST CALCULATION RULES
You MUST follow these exact formulas. DO NOT modify or infer different logic.

{cost_rules_text}

🔴 IMPORTANT RULES:
1. **NEVER mix SP and RI costs** - calculate them separately
2. **ALWAYS filter by line_item_line_item_type** as specified in the formulas
3. **Use SUM() aggregation** for all cost calculations
4. **Follow Redshift syntax requirements**:
   - Date format: TO_CHAR(date_column, 'YYYY-MM')
   - String split: SPLIT_PART function
   - Decimal type: CAST as DECIMAL(24,8)
5. **Always include date filters** using line_item_usage_start_date

📊 COST FORMULA EXAMPLES:

SP Used Cost:
```sql
SUM(CASE WHEN line_item_line_item_type = 'SavingsPlanCoveredUsage' 
    THEN savings_plan_savings_plan_effective_cost ELSE 0 END) as sp_used_cost
```

SP Unused Cost:
```sql
SUM(CASE WHEN line_item_line_item_type = 'SavingsPlanRecurringFee' 
    THEN (savings_plan_total_commitment_to_date - savings_plan_used_commitment) ELSE 0 END) as sp_unused_cost
```

RI Used Cost:
```sql
SUM(CASE WHEN line_item_line_item_type = 'DiscountedUsage' 
    THEN reservation_effective_cost ELSE 0 END) as ri_used_cost
```

RI Unused Cost:
```sql
SUM(CASE WHEN line_item_line_item_type = 'RIFee' 
    THEN (reservation_unused_amortized_upfront_fee_for_billing_period + reservation_unused_recurring_fee) ELSE 0 END) as ri_unused_cost
```

Available resources:
- Cluster: redshift
- Database: cur_database
- Schema: cur
- Table: cost_and_usage_report

Common columns:
- line_item_usage_account_id: AWS account ID
- line_item_usage_start_date: Usage date (ALWAYS USE FOR FILTERING!)
- line_item_line_item_type: Line item type (CRITICAL for cost calculation)
- line_item_product_code: AWS service (EC2, S3, etc)
- savings_plan_savings_plan_a_r_n: SP ARN
- reservation_reservation_a_r_n: RI ARN
"""
    
    hooks = []
    if with_hook:
        hooks.append(ToolCallLimitHook(soft_limit=5))
    if query_log_hook:
        hooks.append(query_log_hook)
    
    return Agent(tools=tools, system_prompt=system_prompt, hooks=hooks)

# 5. 유틸리티 함수
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

# 6. 세션 상태 초기화
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

if "cost_rules" not in st.session_state:
    st.session_state.cost_rules = load_cost_rules()
    if st.session_state.cost_rules:
        st.success("✅ 비용 계산 로직 로드 완료!")

if "interrupt_state" not in st.session_state:
    st.session_state.interrupt_state = None

# 7. 사이드바 구성
with st.sidebar:
    st.header("💡 예시 질문")
    
    examples = [
        {
            "short": "RI/SP 현황 (최근 3개월)",
            "full": "삼성클라우드(775638497521), 삼성페이(622803788537), 삼성헬스(657197638512) 서비스의 최근 3개월 EC2 인스턴스를 Reserved Instance와 Savings Plan 상황 알려줘"
        },
        {
            "short": "SP/RI 낭비 비용 분석",
            "full": "최근 3개월간 Savings Plan과 Reserved Instance의 사용 비용(used)과 낭비 비용(unused)을 각각 계산해서 보여줘"
        },
        {
            "short": "비용 절감 플랜 (최근 3개월)",
            "full": "삼성클라우드(775638497521) 비용 절감 플랜과 연간 절감 가능금액을 최근 3개월 데이터기반으로 알려줘"
        },
        {
            "short": "약정 효율성 분석",
            "full": "최근 3개월 SP와 RI의 실제 절감액을 계산해줘. (온디맨드 대비 비용과 실제 지불 비용 차이)"
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
    
    # 비용 계산 로직 표시
    if st.session_state.cost_rules:
        with st.expander("📖 비용 계산 로직 보기"):
            st.json(st.session_state.cost_rules)
    
    st.divider()
    
    if st.button("🗑️ 대화 초기화", use_container_width=True):
        st.session_state.messages = []
        st.session_state.interrupt_state = None
        st.rerun()

# 8. 메인 채팅 영역
st.divider()

# 메시지 히스토리 출력
for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        
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

# 9. AI 응답 로직
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user" and not st.session_state.interrupt_state:
    with st.chat_message("assistant"):
        status_container = st.status("🔍 분석 중...", expanded=True)
        
        try:
            max_pairs = st.session_state.get("context_pairs", 3)
            conversation_context = get_conversation_context(st.session_state.messages, max_pairs=max_pairs)
            current_question = st.session_state.messages[-1]["content"]
            
            if conversation_context and max_pairs > 0:
                full_prompt = f"""[이전 대화]\n{conversation_context}\n\n[현재 질문]\n{current_question}\n\n위 대화 맥락을 고려하여 현재 질문에 답변해주세요."""
            else:
                full_prompt = current_question
            
            query_log_hook = RealTimeQueryLogHook(status_container)
            
            agent = create_agent(
                st.session_state.redshift_client, 
                st.session_state.cost_rules,
                query_log_hook=query_log_hook
            )
            result_obj = agent(full_prompt)
            
            status_container.update(label="✅ 분석 완료!", state="complete")
            
            response_text = getattr(result_obj, 'text', str(result_obj))
            st.markdown(response_text)
            
            msg_with_log = {
                "role": "assistant", 
                "content": response_text,
                "query_log": query_log_hook.queries
            }
            st.session_state.messages.append(msg_with_log)
            
            if query_log_hook.queries:
                with st.expander(f"📊 실행된 전체 쿼리 보기 ({len(query_log_hook.queries)}개)"):
                    for i, q in enumerate(query_log_hook.queries):
                        st.write(f"**쿼리 {i+1}** - {q['status']} ({q['timestamp']})")
                        st.code(q["sql"], language="sql")
            
        except Interrupt as interrupt:
            status_container.update(label="⚠️ 중간 확인 필요", state="error")
            
            summary_prompt = interrupt.data.get("partial_summary_prompt")
            
            temp_agent = create_agent(
                st.session_state.redshift_client,
                st.session_state.cost_rules,
                with_hook=False
            )
            temp_agent.messages = agent.messages.copy()
            partial_result = temp_agent(summary_prompt)
            partial_summary = getattr(partial_result, 'text', str(partial_result))
            
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
