import streamlit as st
from mcp import stdio_client, StdioServerParameters
from strands import Agent
from strands.tools.mcp import MCPClient
# from strands.types import ... (이 줄을 삭제하여 에러 방지)

st.set_page_config(page_title="Redshift Cost Agent", page_icon="💰", layout="wide")
st.title("💰 AWS Cost Analysis Agent (Safe Mode)")

# --- 1. 초기화 및 캐싱 ---

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

# --- 2. 에이전트 설정 ---

def get_agent():
    """Agent 인스턴스 생성"""
    tools = get_cached_tools()
    
    system_prompt = """You are an expert AWS cost analyst leveraging Redshift data.

Data Schema:
- Table: cost_and_usage (Schema: cur)
- Key Columns: line_item_usage_account_id, line_item_usage_start_date, line_item_product_code, line_item_unblended_cost

Analysis Guidelines:
1. Always filter by 'line_item_usage_start_date'.
2. Aggregate data using SUM/COUNT to provide meaningful insights.
3. Generate efficient SQL queries.
"""
    # Strands Agent 생성
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

# --- 5. 메인 로직 ---

# 채팅 히스토리 렌더링
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 사용자 입력 처리
if prompt := st.chat_input("AWS 비용 질문을 입력하세요..."):
    
    # 1. 확인 대기 상태 처리 (예산 초과 후 승인 여부)
    if st.session_state.waiting_for_confirmation:
        if any(x in prompt.lower() for x in ['y', '예', '응', 'yes', 'go']):
            st.session_state.query_count = 0 # 카운트 리셋
            st.session_state.waiting_for_confirmation = False
            st.session_state.messages.append({"role": "user", "content": prompt})
            st.chat_message("user").markdown(prompt)
            
            with st.chat_message("assistant"):
                st.info("✅ 승인되었습니다. 다시 질문해주시면 분석을 수행합니다.")
                # (구조상 직전 컨텍스트를 이어가려면 메시지 처리가 복잡해지므로, 
                # 여기서는 UX적으로 다시 질문을 유도하거나 재실행하는 흐름으로 안내)
        else:
            st.warning("작업이 중단되었습니다.")
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
            
            try:
                # Strands Stream 실행
                stream = agent.run(
                    messages=st.session_state.messages,
                    stream=True
                )
                
                # --- CORE LOOP: Import 없는 안전한 방식 ---
                for event in stream:
                    
                    # 이벤트의 클래스 이름을 문자열로 확인 (Import 에러 방지)
                    event_type_name = type(event).__name__
                    
                    # Case A: 도구 사용(쿼리) 시도 감지
                    # 클래스 이름에 'Tool'과 'Use' 또는 'Call'이 포함되어 있으면 잡음
                    if "Tool" in event_type_name and ("Use" in event_type_name or "Call" in event_type_name):
                        
                        # 예산 체크
                        if st.session_state.query_count >= max_queries:
                            st.session_state.waiting_for_confirmation = True
                            warning_msg = "\n\n⛔ **쿼리 예산 한도 도달!** 추가 진행을 승인하시겠습니까? (예/아니오)"
                            full_response += warning_msg
                            message_placeholder.markdown(full_response)
                            
                            # ★ Loop 강제 중단 (실제 쿼리 실행 차단)
                            break 
                        
                        # 예산 내라면 카운트 증가
                        st.session_state.query_count += 1
                        
                        # 도구 이름 추출 (안전하게)
                        tool_name = getattr(event, 'tool_name', 'Query Tool')
                        
                        # UI 업데이트
                        status_msg = f"\n\n*🔍 [Query 실행] {tool_name} (예산: {st.session_state.query_count}/{max_queries})*\n\n"
                        full_response += status_msg
                        message_placeholder.markdown(full_response)

                    # Case B: 일반 텍스트 생성 (속성 체크)
                    elif hasattr(event, 'text'):
                        full_response += event.text
                        message_placeholder.markdown(full_response + "▌")
                    
                    # Case C: 문자열 자체가 들어오는 경우
                    elif isinstance(event, str):
                        full_response += event
                        message_placeholder.markdown(full_response + "▌")
                    
                    # 그 외 이벤트는 무시
                    else:
                        pass

                message_placeholder.markdown(full_response)
                st.session_state.messages.append({"role": "assistant", "content": full_response})

                # 승인 대기 상태가 되었으면 Rerun
                if st.session_state.waiting_for_confirmation:
                    st.rerun()

            except Exception as e:
                st.error(f"시스템 오류: {str(e)}")
                st.code(f"Event Debug Info: {type(event).__name__} - {event}")
