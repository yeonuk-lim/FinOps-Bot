import streamlit as st
import os
from mcp import stdio_client, StdioServerParameters
from strands import Agent
from strands.tools.mcp import MCPClient

st.set_page_config(page_title="MCP Agent Demo", page_icon="🤖", layout="wide")
st.title("🤖 MCP Agent with AWS Documentation")

uvx_path = os.path.expanduser("~/.local/bin/uvx")

@st.cache_resource
def init_mcp_client():
    client = MCPClient(lambda: stdio_client(
        StdioServerParameters(command=uvx_path, args=["awslabs.aws-documentation-mcp-server@latest"])
    ))
    client.start()
    return client

@st.cache_resource
def init_agent(_mcp_client):
    tools = _mcp_client.list_tools_sync()
    return Agent(tools=tools)

if "messages" not in st.session_state:
    st.session_state.messages = []

if "mcp_client" not in st.session_state:
    with st.spinner("MCP 서버 연결 중..."):
        st.session_state.mcp_client = init_mcp_client()
        st.session_state.agent = init_agent(st.session_state.mcp_client)
    st.success("✅ 연결 완료!")

# 사이드바 - 예시 질문만
with st.sidebar:
    st.header("💡 예시 질문")
    examples = [
        "AWS Lambda가 뭐야?",
        "CUR이 뭐야? 예시 쿼리랑 설명 알려줘",
        "S3 버킷 생성 방법",
        "EC2 인스턴스 타입 비교"
    ]
    
    for q in examples:
        if st.button(q, key=q, use_container_width=True):
            st.session_state.messages.append({"role": "user", "content": q})
            st.rerun()
    
    st.divider()
    
    if st.button("🗑️ 대화 초기화", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# 메인 채팅 영역
st.divider()

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if prompt := st.chat_input("질문을 입력하세요..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    
    with st.chat_message("user"):
        st.markdown(prompt)
    
    with st.chat_message("assistant"):
        with st.spinner("생각 중..."):
            response = st.session_state.agent(prompt)
        st.markdown(response)
        st.session_state.messages.append({"role": "assistant", "content": response})
