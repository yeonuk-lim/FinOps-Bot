import streamlit as st
from mcp import stdio_client, StdioServerParameters
from strands import Agent
from strands.tools.mcp import MCPClient

# 페이지 기본 설정
st.set_page_config(page_title="Redshift Cost Agent", page_icon="💰", layout="wide")
st.title("💰 AWS Cost Analysis Agent")

# --- 1. 초기화 및 리소스 캐싱 ---

@st.cache_resource
def get_mcp_client():
    """Redshift MCP 클라이언트 연결 (세션 당 1회)"""
    # 실제 환경에 맞게 uvx 경로/설정이 올바른지 확인 필요
    client = MCPClient(lambda: stdio_client(
        StdioServerParameters(
            command="uvx",
            args=["awslabs.redshift-mcp-server@latest"],
            env={"AWS_DEFAULT_REGION": "us-east-1", "FASTMCP_LOG_LEVEL": "ERROR"}
        )
    ))
    client.start()
    return client

@st.cache_resource
def get_cached_tools():
    """툴 리스트 캐싱 (매 턴 API 호출 오버헤드 방지)"""
    client = get_mcp_client()
    return client.list_tools_sync()

# --- 2. 에이전트 정의 ---

def get_agent():
    """Agent 인스턴스 생성"""
    tools = get_cached_tools()
    
    # 시스템 프롬프트: 예산 로직은 제외하고 분석 지침에만 집중
    system_prompt = """You are an expert AWS cost analyst.
Data Source: Redshift 'cost_and_usage' table (schema: cur).
Columns: line_item_usage_account_id, line_item_usage_start_date, line_item_product_code, line_item_unblended_cost.

Guidelines:
1. Always filter queries by 'line_item_usage_start_date'.
2. Use SUM/COUNT/GROUP BY for aggregation.
3. Be concise in explanations."""
    
    return Agent(model="anthropic.claude-3-5-sonnet-v2:0", tools=tools, system_prompt=system_prompt)

# --- 3. 세션 상태 초기화 ---

if "messages" not in st.session_state:
    st.session_state.messages = []

if "query_count" not in st.session_state:
    st.session_state.query_count = 0

if "waiting_for_confirmation" not in st.session_state:
    st.session_state.waiting_for_confirmation = False

# --- 4. 사이드바 (예시 질문 & 제어 패널) ---

with st.sidebar:
    # [복구됨] 예시 질문 섹션
    st.header("💡 예시 질문")
    
    examples = [
        {
            "short": "RI/SP 현황 (3개 계정)",
            "full": "삼성클라우드, 삼성페이, 삼성헬스 서비스의 EC2 인스턴스 RI/SP 현재 커버리지 현황 알려줘"
        },
        {
            "short": "비용 절감 플랜",
            "full": "주요 계정의 비용 절감 플랜과 연간 절감 가능 금액을 분석해줘"
        },
        {
            "short": "월별 비용 급증 분석",
            "full": "지난달 대비 비용이 20% 이상 증가한 계정과 서비스 원인 분석해줘"
        }
    ]
    
    for i, example in enumerate(examples):
        if st.button(example["short"], key=f"ex_{i}", use_container_width=True, help=example["full"]):
            # 예시 클릭 시 바로 질문 입력 처리
            st.session_state.messages.append({"role": "user", "content": example["full"]})
            st.session_state.waiting_for_confirmation = False
            st.rerun()
            
    st.divider()

    # 예산 모니터링 UI
    st.header("📊 쿼리 예산 제어")
    max_queries = 5
    
    col1, col2 = st.columns(2)
    col1.metric("사용됨", f"{st.session_state.query_count}")
    col2.metric("한도", f"{max_queries}")
    
    st.progress(min(st.session_state.query_count / max_queries, 1.0))
    
    if st.session_state.query_count >= max_queries:
        st.warning("⚠️ 예산 도달 (승인 필요)")
    
    st.divider()
    
    if st.button("🗑️ 대화 초기화", use_container_width=True):
        st.session_state.messages = []
        st.session_state.query_count = 0
        st.session_state.waiting_for_confirmation = False
        st.rerun()

# --- 5. 메인 채팅 로직 ---

# 히스토리 표시
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 입력 처리
if prompt := st.chat_input("AWS 비용 질문을 입력하세요..."):
    
    # Case 1: 예산 초과 승인 대기 중
    if st.session_state.waiting_for_confirmation:
        if any(x in prompt.lower() for x in ['y', '예', '응', 'yes', 'go']):
            st.session_state.query_count = 0 # 리셋
            st.session_state.waiting_for_confirmation = False
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("assistant"):
                st.info("✅ 승인되었습니다. 질문을 분석합니다.")
                # 여기서는 흐름상 사용자 입력을 다시 처리하도록 유도하거나 로직을 재호출해야 함
                # 간단한 처리를 위해 정보 메시지만 출력하고 다음 턴으로 넘김
        else:
            st.warning("작업이 취소되었습니다.")
            st.session_state.waiting_for_confirmation = False
            st.stop()

    # Case 2: 정상 대화 진행
    else:
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            message_placeholder = st.empty()
            full_response = ""
            
            try:
                agent = get_agent()
                
                # [수정됨] agent.run() -> agent() 호출 (Strands 방식)
                stream = agent(
                    messages=st.session_state.messages,
                    stream=True
                )
                
                # --- CORE LOOP: Code-Level Control ---
                for event in stream:
                    
                    # [수정됨] Import 없이 클래스 이름 문자열로 확인 (안전장치)
                    event_type = type(event).__name__
                    
                    # A. 도구 사용(쿼리) 감지
                    if "Tool" in event_type and ("Use" in event_type or "Call" in event_type):
                        
                        # 예산 초과 체크
                        if st.session_state.query_count >= max_queries:
                            st.session_state.waiting_for_confirmation = True
                            warning_msg = "\n\n⛔ **쿼리 예산 한도 도달!** 추가 진행을 승인하시겠습니까? (예/아니오)"
                            full_response += warning_msg
                            message_placeholder.markdown(full_response)
                            
                            # ★ Loop Break: 물리적으로 쿼리 실행을 막음
                            break 
                        
                        # 예산 통과 시
                        st.session_state.query_count += 1
                        tool_name = getattr(event, 'tool_name', 'Query Tool')
                        
                        # UI 피드백
                        status = f"\n\n*🔍 [Query 실행] {tool_name} (누적: {st.session_state.query_count}/{max_queries})*\n\n"
                        full_response += status
                        message_placeholder.markdown(full_response)

                    # B. 텍스트 생성
                    elif hasattr(event, 'text'):
                        full_response += event.text
                        message_placeholder.markdown(full_response + "▌")
                    
                    # C. 문자열 스트림
                    elif isinstance(event, str):
                        full_response += event
                        message_placeholder.markdown(full_response + "▌")

                # 최종 응답 표시 및 저장
                message_placeholder.markdown(full_response)
                st.session_state.messages.append({"role": "assistant", "content": full_response})
                
                # 승인 대기 상태 진입 시 UI 갱신
                if st.session_state.waiting_for_confirmation:
                    st.rerun()

            except Exception as e:
                st.error(f"에러 발생: {str(e)}")
                st.code(f"Details: {type(e).__name__}")
