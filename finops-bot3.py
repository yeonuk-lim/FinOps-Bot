import streamlit as st
import os
from mcp import stdio_client, StdioServerParameters
from strands import Agent
from strands.tools.mcp import MCPClient

# 1. 페이지 설정 (가장 먼저 실행)
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
def create_agent(mcp_client, query_count=0, max_queries=5):
    """Agent 생성 with query limit awareness"""
    tools = mcp_client.list_tools_sync()
    
    remaining_queries = max_queries - query_count
    
    system_prompt = f"""You are an AWS cost analysis assistant with access to Redshift.

🔢 QUERY BUDGET SYSTEM:
- **Current query count: {query_count}**
- **Remaining queries: {remaining_queries}**
- **Initial budget: {max_queries} queries per question**

📋 RULES:
1. **If remaining queries > 0**: You can execute queries normally
2. **If remaining queries = 0**: You MUST ask user for permission before any query
   - Say: "이 질문에 답하려면 추가로 N개의 쿼리가 필요합니다. 계속 진행할까요? (예/아니오)"
   - Wait for user confirmation
   - Only proceed if user says "예", "yes", "계속", "진행" or similar

3. **Query Efficiency Guidelines**:
   - Write ONE comprehensive query instead of multiple simple queries
   - Always include date filters (line_item_usage_start_date)
   - Combine conditions in WHERE clause
   - Use subqueries/CTEs for complex analysis
   - Get all needed data in single query when possible

Available resources:
- Cluster: redshift
- Database: cur_database
- Schema: public
- Table: cost_and_usage_report

Common columns:
- line_item_usage_account_id: AWS account ID
- line_item_usage_start_date: Usage date (ALWAYS USE FOR FILTERING!)
- line_item_product_code: AWS service (EC2, S3, etc)
- line_item_unblended_cost: Cost amount
- line_item_usage_type: Usage type

Example efficient query (use this pattern):
SELECT 
  line_item_usage_account_id,
  line_item_product_code,
  SUM(line_item_unblended_cost) as total_cost,
  COUNT(DISTINCT line_item_usage_start_date) as days_used
FROM cost_and_usage
WHERE line_item_usage_start_date >= '2025-09-01'
  AND line_item_usage_start_date < '2025-10-01'
  AND line_item_unblended_cost > 0
GROUP BY line_item_usage_account_id, line_item_product_code
ORDER BY total_cost DESC
LIMIT 10;

⚠️ IMPORTANT:
- If you've used all {max_queries} queries, ASK USER before proceeding
- Be transparent about query usage
- Suggest more specific questions to reduce query needs"""
    
    return Agent(tools=tools, system_prompt=system_prompt)

# 4. 유틸리티 함수들
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

def is_user_confirmation(text):
    text_lower = text.lower().strip()
    confirmation_keywords = [
        '예', 'yes', 'y', '네', '응', '그래', '계속', '진행', 
        'ok', 'okay', '좋아', '알겠어', '해줘', '부탁해'
    ]
    return any(keyword in text_lower for keyword in confirmation_keywords)

# [수정됨] 사이드바 버튼 클릭 처리를 위한 콜백 함수
def handle_example_click(message_text):
    st.session_state.messages.append({"role": "user", "content": message_text})
    st.session_state.waiting_for_confirmation = False

# 5. 세션 상태 초기화
if "messages" not in st.session_state:
    st.session_state.messages = []

if "query_count" not in st.session_state:
    st.session_state.query_count = 0

if "waiting_for_confirmation" not in st.session_state:
    st.session_state.waiting_for_confirmation = False

if "redshift_client" not in st.session_state:
    with st.spinner("Redshift 연결 중..."):
        try:
            st.session_state.redshift_client = init_redshift_client()
            st.success("✅ Redshift 연결 완료!")
        except Exception as e:
            st.error(f"❌ 연결 실패: {str(e)}")
            st.stop()

# 6. 사이드바 구성
with st.sidebar:
    st.header("💡 예시 질문")
    
    examples = [
        {
            "short": "RI/SP 현황 (3개 계정)",
            "full": "삼성클라우드(775638497521), 삼성페이(622803788537), 삼성헬스(657197638512) 서비스의 최근 3개월 EC2 인스턴스를 Reserved Instance와 Savings Plan 상황 알려줘"
        },
        {
            "short": "비용 절감 플랜",
            "full": "삼성클라우드(775638497521) 비용 절감 플랜과 연간 절감 가능금액을 최근 3개월 데이터기반으로 알려줘"
        },
        {
            "short": "사용자당 비용 분석",
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
    
    # [수정됨] on_click 콜백을 사용하여 버튼 동작 개선
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
    
    # 쿼리 사용량 모니터링
    st.subheader("📊 쿼리 사용량")
    max_queries = 5
    
    col1, col2 = st.columns(2)
    with col1:
        st.metric("사용", f"{st.session_state.query_count}회")
    with col2:
        st.metric("제한", f"{max_queries}회")
    
    progress = min(st.session_state.query_count / max_queries, 1.0)
    st.progress(progress)
    
    if st.session_state.query_count >= max_queries:
        st.warning("⚠️ 기본 쿼리 제한 도달\n추가 쿼리는 확인 후 진행")
    
    st.divider()
    
    st.subheader("⚙️ 설정")
    context_pairs = st.slider("대화 컨텍스트 유지 개수", 0, 10, 3)
    st.session_state.context_pairs = context_pairs
    
    st.divider()
    
    if st.button("🗑️ 대화 초기화", use_container_width=True):
        st.session_state.messages = []
        st.session_state.query_count = 0
        st.session_state.waiting_for_confirmation = False
        st.rerun()

# 7. 메인 채팅 영역
st.divider()

# 메시지 히스토리 출력
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 사용자 입력 처리 (채팅창 입력)
if prompt := st.chat_input("AWS 비용에 대해 질문하세요..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.rerun()

# 8. AI 응답 로직 (마지막 메시지가 user일 때 실행)
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
    with st.chat_message("assistant"):
        with st.spinner("쿼리 실행 중..."):
            try:
                user_message = st.session_state.messages[-1]["content"]
                
                # 확인 대기 로직
                if st.session_state.waiting_for_confirmation:
                    if is_user_confirmation(user_message):
                        st.session_state.query_count = 0
                        st.session_state.waiting_for_confirmation = False
                        response_text = "알겠습니다. 추가 쿼리를 진행하겠습니다."
                        st.info(response_text)
                        st.session_state.messages.append({"role": "assistant", "content": response_text})
                        st.rerun()
                    else:
                        st.session_state.waiting_for_confirmation = False
                        response_text = "알겠습니다. 추가 쿼리 없이 현재까지의 정보로 답변드리겠습니다."
                        st.markdown(response_text)
                        st.session_state.messages.append({"role": "assistant", "content": response_text})
                        st.stop()
                
                # 컨텍스트 구성
                max_pairs = st.session_state.get("context_pairs", 3)
                conversation_context = get_conversation_context(st.session_state.messages, max_pairs=max_pairs)
                current_question = st.session_state.messages[-1]["content"]
                
                if conversation_context and max_pairs > 0:
                    full_prompt = f"""[이전 대화]\n{conversation_context}\n\n[현재 질문]\n{current_question}\n\n위 대화 맥락을 고려하여 현재 질문에 답변해주세요."""
                else:
                    full_prompt = current_question
                
                # Agent 생성
                agent = create_agent(
                    st.session_state.redshift_client,
                    query_count=st.session_state.query_count,
                    max_queries=5
                )
                
                # Agent 실행
                result_obj = agent(full_prompt)
                
                # [수정됨] AgentResult 객체에서 텍스트 추출 (속성명이 .text라고 가정)
                # strands 버전에 따라 .text 혹은 .content 일 수 있습니다.
                response_text = getattr(result_obj, 'text', str(result_obj))
                
                # 쿼리 카운트 계산
                estimated_queries = response_text.count("SELECT") if "SELECT" in response_text else 1
                st.session_state.query_count += estimated_queries
                
                # 확인 필요 여부 체크
                if "추가로" in response_text and "쿼리가 필요합니다" in response_text and "계속 진행할까요" in response_text:
                    st.session_state.waiting_for_confirmation = True
                
                # 결과 출력
                st.markdown(response_text)
                
                if estimated_queries > 0:
                    st.caption(f"📊 이번 응답에서 약 {estimated_queries}개의 쿼리 실행됨 (총 {st.session_state.query_count}개)")
                
                # 대화 기록 저장
                st.session_state.messages.append({"role": "assistant", "content": response_text})
                
            except Exception as e:
                error_msg = f"❌ 오류 발생: {str(e)}"
                st.error(error_msg)
                st.session_state.messages.append({"role": "assistant", "content": error_msg})
