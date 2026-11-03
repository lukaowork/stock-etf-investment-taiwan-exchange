"""登入驗證共用模組（app.py 與 pages/ 多頁共用）。

密碼一律存放於 .streamlit/secrets.toml，程式碼中不出現明文（agents.md 安全規則）。
"""

import streamlit as st


def get_configured_password() -> str:
    """從 Streamlit secrets 讀取存取密碼；未設定時回傳空字串。"""
    try:
        pwd = st.secrets["app_password"]
        return str(pwd)
    except Exception:
        return ""


def check_password() -> bool:
    """檢查使用者是否已通過登入驗證。

    未通過時於當頁渲染登入畫面並回傳 False，由呼叫端以 st.stop() 中斷後續程式。
    驗證狀態存於全域 st.session_state["authenticated"]，各分頁共用。
    """
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if st.session_state["authenticated"]:
        return True

    configured_password = get_configured_password()
    if not configured_password:
        st.error(
            "⚠️ 未設定存取密碼。請在 `.streamlit/secrets.toml` 中加入：\n\n"
            "```toml\napp_password = \"你的密碼\"\n```"
        )
        return False

    st.markdown("<h1 style='text-align: center;'>🔒 登入個人投資分析系統</h1>", unsafe_allow_html=True)
    st.markdown("---")

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        password_input = st.text_input("請輸入系統存取密碼：", type="password")
        if st.button("登入系統", use_container_width=True):
            if password_input == configured_password:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("❌ 密碼錯誤！請重新輸入。")
    return False
