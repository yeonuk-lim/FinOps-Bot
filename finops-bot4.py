import streamlit as st
from mcp import stdio_client, StdioServerParameters
from strands import Agent
from strands.tools.mcp import MCPClient
# Strands의 이벤트/블록 타입 (가상의 SDK 구조 가정)
from strands.types import ToolUseBlock, TextBlock

st.set_page_config(page_title="Redshift Cost Agent", page_icon="💰", layout="wide")
st.title("💰 AWS Cost Analysis Agent (Strands Native)")

# --- 1. 초기화 및 캐싱 (최적화) ---

@st.cache_resource
def get_mcp_client():
    """Redshift MCP 클라이언트 연결 (세션 당 1회)"""
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
    """툴 리스트 캐싱 (매 턴 호출 방지)"""
    client = get_mcp_client()
    return client.list_tools_sync()

# --- 2. 에이전트 설정 (프롬프트 다이어트) ---

def get_agent():
    """
    Agent 인스턴스 생성. 
    예산 관리 로직은 프롬프트에서 제거하고 순수 분석 지침만 남김.
    """
    tools = get_cached_tools()
    
    system_prompt = """You are an expert AWS cost analyst leveraging Redshift data.

Data Schema:
- Table: cost_and_usage (Schema: cur)
- Key Columns: line_item_usage_account_id, line_item_usage_start_date, line_item_product_code, line_item_unblended_cost

Analysis Guidelines:
1. Always filter by 'line_item_usage_start_date'.
2. Aggregate data using SUM/COUNT to provide meaningful insights.
3. When a user asks about cost trends, analyze the last 3 months unless specified.
4. Generate efficient SQL queries.
"""
    # Strands Agent는 상태를 내부적으로 가지지 않고, run 시점에 messages를 받도록 설계됨
    return Agent(model="anthropic.claude-3-5-sonnet-v2:0", tools=tools, system_prompt=system_prompt)

# --- 3. 세션 상태 관리 ---

if "messages" not in st.session_state:
    st.session_state.messages = []

if "query_count" not in st.session_state:
    st.session_state.query_count = 0

if "waiting_for_confirmation" not in st.session_state:
    st.session_state.waiting_for_confirmation = False

# --- 4. 사이드바 (UI) ---

with st.sidebar:
    st.header("📊 쿼리 예산 제어")
    max_queries = 5
    
    col1, col2 = st.columns(2)
    col1.metric("사용됨", f"{st.session_state.query_count}")
    col2.metric("한도", f"{max_queries}")
    
    st.progress(min(st.session_state.query_count / max_queries, 1.0))
    
    if st.button("초기화"):
        st.session_state.messages = []
        st.session_state.query_count = 0
        st.session_state.waiting_for_confirmation = False
        st.rerun()

# --- 5. 메인 로직 (Generator Loop 제어) ---

# 채팅 히스토리 렌더링
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 사용자 입력 처리
if prompt := st.chat_input("AWS 비용 질문을 입력하세요..."):
    
    # 1. 확인 대기 상태 처리
    if st.session_state.waiting_for_confirmation:
        if any(x in prompt.lower() for x in ['y', '예', '응', 'yes', 'go']):
            st.session_state.query_count = 0 # 카운트 리셋
            st.session_state.waiting_for_confirmation = False
            st.session_state.messages.append({"role": "user", "content": prompt})
            st.chat_message("user").markdown(prompt)
            
            # 승인 메시지 후 AI가 '이전 질문'을 다시 수행하도록 유도하려면
            # 실제로는 마지막 AI 턴을 재생성하거나 해야 하지만, 
            # 여기서는 간단히 "승인되었으니 답변을 생성합니다" 로직으로 진행
            with st.chat_message("assistant"):
                st.info("✅ 승인되었습니다. 분석을 계속합니다.")
                # (심화 구현 시: 직전 ToolCall을 재실행하는 로직이 필요함)
        else:
            st.warning("작업이 취소되었습니다.")
            st.session_state.waiting_for_confirmation = False
            st.stop()

    else:
        # 2. 일반 대화 진행
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            message_placeholder = st.empty()
            full_response = ""
            
            # Agent 가져오기
            agent = get_agent()
            
            # Strands Stream 실행 (History 객체 그대로 전달)
            # stream=True를 통해 토큰/이벤트 단위 제어
            stream = agent.run(
                messages=st.session_state.messages,
                stream=True
            )
            
            try:
                # --- CORE LOOP: Code-Level Control ---
                for event in stream:
                    
                    # Case A: 도구 사용(쿼리) 시도 감지
                    if isinstance(event, ToolUseBlock):
                        # 예산 체크
                        if st.session_state.query_count >= max_queries:
                            st.session_state.waiting_for_confirmation = True
                            warning_msg = "\n\n⛔ **쿼리 예산 한도 도달!** 추가 진행을 승인하시겠습니까? (예/아니오)"
                            full_response += warning_msg
                            message_placeholder.markdown(full_response)
                            
                            # ★ 여기서 Loop 강제 중단 (쿼리 실행 막음)
                            # Generator를 멈추면 실제 Tool Execution이 발생하지 않음
                            break 
                        
                        # 예산 내라면 카운트 증가 후 진행 허용
                        st.session_state.query_count += 1
                        # (Optional) UI에 쿼리 실행 중임을 표시
                        full_response += f"\n\n*🔍 [Query 실행] {event.tool_name}...*\n\n"
                        message_placeholder.markdown(full_response)

                    # Case B: 일반 텍스트 생성
                    elif isinstance(event, TextBlock):
                        full_response += event.text
                        message_placeholder.markdown(full_response + "▌")
                    
                    # Case C: 그냥 텍스트 스트림 (Strands 버전에 따라 다름)
                    elif isinstance(event, str):
                        full_response += event
                        message_placeholder.markdown(full_response + "▌")

                message_placeholder.markdown(full_response)
                st.session_state.messages.append({"role": "assistant", "content": full_response})

                # 승인 대기 상태면 Rerun 하여 입력창 활성화
                if st.session_state.waiting_for_confirmation:
                    st.rerun()

            except Exception as e:
                st.error(f"에러 발생: {e}")

