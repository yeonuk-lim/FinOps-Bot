import streamlit as st
import os
from mcp import stdio_client, StdioServerParameters
from strands import Agent
# [변경] Hook 관련 최신 모듈 임포트
from strands.hooks import HookProvider, HookRegistry, AfterToolCallEvent
from strands.tools.mcp import MCPClient

# -------------------------------------------------------------------------
# 1. 커스텀 Hook 정의 (최신 버전 문법 적용)
# -------------------------------------------------------------------------
class ToolCallLimitHook(HookProvider):
    def __init__(self, soft_limit=5):
        self.soft_limit = soft_limit
        self.tool_call_count = 0
    
    # [변경] HookRegistry에 콜백 등록
    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        # 툴 실행 직후 카운트를 확인하기 위해 AfterToolCallEvent 사용
        registry.add_callback(AfterToolCallEvent, self.check_limit)
        
    def check_limit(self, event: AfterToolCallEvent) -> None:
        self.tool_call_count += 1
        
        if self.tool_call_count >= self.soft_limit:
            # [변경] 예외 발생(raise) 대신 event.interrupt() 호출
            # name: 인터럽트 식별자, reason: 전달할 데이터
            event.interrupt(
                name="tool_limit_reached",
                reason={
                    "tool_calls": self.tool_call_count,
                    "partial_summary_prompt": "지금까지 수집한 정보를 바탕으로 현재까지 알 수 있는 내용을 간단히 요약해주세요."
                }
            )

# -------------------------------------------------------------------------
# 2. Streamlit 앱 설정
# -------------------------------------------------------------------------
st.set_page_config(page_title="Redshift Query Chatbot", page_icon="💰", layout="wide")
st.title("💰 AWS Cost Analysis Chatbot")

# -------------------------------------------------------------------------
# 3. Redshift 클라이언트 초기화
# -------------------------------------------------------------------------
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

# -------------------------------------------------------------------------
# 4. Agent 생성 함수
# -------------------------------------------------------------------------
def create_agent(mcp_client, with_hook=True):
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
    
    hooks = [ToolCallLimitHook(soft_limit=5)] if with_hook else []
    
    # hooks는 리스트 형태로 전달
    return Agent(tools=tools, system_prompt=system_prompt, hooks=hooks)

# -------------------------------------------------------------------------
# 5. 유틸리티 함수
# -------------------------------------------------------------------------
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

# -------------------------------------------------------------------------
# 6. 세션 상태 초기화
# -------------------------------------------------------------------------
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

# -------------------------------------------------------------------------
# 7. 사이드바 구성
# -------------------------------------------------------------------------
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
        st.rerun()

# -------------------------------------------------------------------------
# 8. 메인 UI 및 로직
# -------------------------------------------------------------------------
st.divider()

# 메시지 히스토리 출력
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# --- [변경] Interrupt 상태 처리 UI ---
if st.session_state.interrupt_state:
    intr_data = st.session_state.interrupt_state
    
    st.info("**📊 중간 결과 (5번 쿼리 완료)**")
    st.markdown(intr_data["partial_summary"])
    
    st.warning(f"⚠️ {intr_data['message']} 계속 진행하시겠습니까?")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("✅ 계속 분석", use_container_width=True):
            with st.spinner("분석 계속 중..."):
                try:
                    # [핵심 변경] agent.resume() -> agent(responses)
                    agent = intr_data["agent"]
                    interrupt_id = intr_data["interrupt_id"]
                    
                    # 인터럽트에 대한 응답 구조 생성
                    responses = [{
                        "interruptResponse": {
                            "interruptId": interrupt_id,
                            "response": "continue" # 훅에서 별도 처리가 필요 없다면 단순 문자열 전달
                        }
                    }]
                    
                    # 에이전트 재실행 (응답 포함)
                    result_obj = agent(responses)
                    
                    # 결과 처리
                    response_text = getattr(result_obj, 'text', str(result_obj))
                    st.markdown(response_text)
                    st.session_state.messages.append({"role": "assistant", "content": response_text})
                    st.session_state.interrupt_state = None
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 오류 발생: {str(e)}")
                    st.session_state.interrupt_state = None
    
    with col2:
        if st.button("❌ 여기서 마무리", use_container_width=True):
            st.session_state.messages.append({
                "role": "assistant", 
                "content": intr_data["partial_summary"]
            })
            st.session_state.interrupt_state = None
            st.success("✅ 중간 결과로 마무리했습니다.")
            st.rerun()

# --- 사용자 입력 처리 ---
if prompt := st.chat_input("AWS 비용에 대해 질문하세요..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.rerun()

# --- [변경] AI 응답 로직 (stop_reason 사용) ---
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user" and not st.session_state.interrupt_state:
    with st.chat_message("assistant"):
        with st.spinner("분석 중..."):
            try:
                # 컨텍스트 구성
                max_pairs = st.session_state.get("context_pairs", 3)
                conversation_context = get_conversation_context(st.session_state.messages, max_pairs=max_pairs)
                current_question = st.session_state.messages[-1]["content"]
                
                if conversation_context and max_pairs > 0:
                    full_prompt = f"""[이전 대화]\n{conversation_context}\n\n[현재 질문]\n{current_question}\n\n위 대화 맥락을 고려하여 현재 질문에 답변해주세요."""
                else:
                    full_prompt = current_question
                
                # Agent 생성
                agent = create_agent(st.session_state.redshift_client)
                
                # [핵심 변경] 실행 후 결과 객체 받기 (try-except 제거)
                result = agent(full_prompt)
                
                # 1. 인터럽트로 멈춘 경우
                if getattr(result, "stop_reason", "") == "interrupt":
                    # 첫 번째 인터럽트 정보 가져오기
                    interrupt_info = result.interrupts[0]
                    
                    if interrupt_info.name == "tool_limit_reached":
                        # 중간 요약 생성
                        summary_prompt = interrupt_info.reason.get("partial_summary_prompt", "요약해주세요.")
                        
                        # 임시 Agent로 중간 요약 생성 (Hook 없이)
                        temp_agent = create_agent(st.session_state.redshift_client, with_hook=False)
                        # 필요한 경우 temp_agent에 메시지 history 복사 로직 추가 가능
                        
                        partial_result = temp_agent(summary_prompt)
                        partial_summary = getattr(partial_result, 'text', str(partial_result))
                        
                        # Interrupt 상태 저장
                        st.session_state.interrupt_state = {
                            "interrupt_id": interrupt_info.id,
                            "message": f"{interrupt_info.reason.get('tool_calls')}번 쿼리를 실행했습니다.",
                            "partial_summary": partial_summary,
                            "agent": agent # agent 객체를 저장하여 나중에 재개
                        }
                        st.rerun()

                # 2. 정상 종료된 경우
                else:
                    response_text = getattr(result, 'text', str(result))
                    st.markdown(response_text)
                    st.session_state.messages.append({"role": "assistant", "content": response_text})
                
            except Exception as e:
                error_msg = f"❌ 오류 발생: {str(e)}"
                st.error(error_msg)
                # 디버깅을 위해 상세 에러 출력
                import traceback
                st.code(traceback.format_exc())
                st.session_state.messages.append({"role": "assistant", "content": error_msg})
