import streamlit as st
import os
from mcp import stdio_client, StdioServerParameters
from strands import Agent
from strands.tools.mcp import MCPClient

st.set_page_config(page_title="Redshift Query Chatbot", page_icon="💰", layout="wide")
st.title("💰 AWS Cost Analysis Chatbot")

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

@st.cache_resource
def init_agent(_mcp_client):
    """Agent 초기화"""
    tools = _mcp_client.list_tools_sync()
    
    system_prompt = """You are an AWS cost analysis assistant with access to Redshift.

Available resources:
- Cluster: cur-analytics
- Database: curdb
- Schema: cur
- Table: cost_and_usage (AWS Cost and Usage Report data)

When users ask about AWS costs:
1. Understand their question
2. Create appropriate SQL query for cur.cost_and_usage table
3. Execute the query on cur-analytics cluster
4. Present results in a clear, conversational way

Common columns in cost_and_usage:
- line_item_usage_account_id: AWS account ID
- line_item_usage_start_date: Usage date
- line_item_product_code: AWS service (EC2, S3, etc)
- line_item_unblended_cost: Cost amount
- line_item_usage_type: Usage type

Always be helpful and explain the results in plain language."""
    
    return Agent(tools=tools, system_prompt=system_prompt)

# 세션 상태 초기화
if "messages" not in st.session_state:
    st.session_state.messages = []

if "redshift_client" not in st.session_state:
    with st.spinner("Redshift 연결 중..."):
        try:
            st.session_state.redshift_client = init_redshift_client()
            st.session_state.agent = init_agent(st.session_state.redshift_client)
            st.success("✅ Redshift 연결 완료!")
        except Exception as e:
            st.error(f"❌ 연결 실패: {str(e)}")
            st.stop()

# 사이드바 - 예시 질문
with st.sidebar:
    st.header("💡 예시 질문")
    
    examples = [
        "상위 10개 계정의 비용을 보여줘",
        "총 계정이 몇 개야?",
        "월별 비용 추이를 알려줘",
        "계정 233255838270의 서비스별 비용은?",
        "가장 비싼 서비스 5개는 뭐야?",
        "7월 데이터만 보여줘"
    ]
    
    for q in examples:
        if st.button(q, key=q, use_container_width=True):
            # [수정됨] 버튼 클릭 시 메시지를 추가하고 rerun만 수행
            # AI 응답 로직은 메인 영역에서 처리됨
            st.session_state.messages.append({"role": "user", "content": q})
            st.rerun()
    
    st.divider()
    
    # 데이터 정보
    st.subheader("📊 데이터 정보")
    st.info("""
    **Cluster:** cur-analytics  
    **Database:** curdb  
    **Schema:** cur  
    **Table:** cost_and_usage
    """)
    
    st.divider()
    
    if st.button("🗑️ 대화 초기화", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# 메인 채팅 영역
st.divider()

# [수정됨] 기존 메시지 표시는 변경 없음 (이 코드는 원래 버그가 없었습니다)
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# [수정됨] AI 응답 로직을 chat_input 블록에서 분리
# 세션의 마지막 메시지가 "user"일 경우 AI 응답을 생성
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
    with st.chat_message("assistant"):
        with st.spinner("쿼리 실행 중..."):
            try:
                # 마지막 사용자 메시지를 프롬프트로 사용
                user_prompt = st.session_state.messages[-1]["content"]
                response = st.session_state.agent(user_prompt)
                st.markdown(response)
                st.session_state.messages.append({"role": "assistant", "content": response})
            except Exception as e:
                error_msg = f"❌ 오류 발생: {str(e)}"
                st.error(error_msg)
                st.session_state.messages.append({"role": "assistant", "content": error_msg})

# [수정됨] 사용자 입력
if prompt := st.chat_input("AWS 비용에 대해 질문하세요..."):
    # 사용자 메시지를 추가하고 rerun
    # AI 응답은 rerun 후 위의 로직 블록에서 처리됨
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.rerun()
