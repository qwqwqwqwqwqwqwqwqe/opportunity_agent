from opportunity_agent.web_app import WEB_DIR


def test_web_ui_asset_is_present():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html += (WEB_DIR / "progress_ui.js").read_text(encoding="utf-8")
    html += (WEB_DIR / "resume_ui.js").read_text(encoding="utf-8")
    html += (WEB_DIR / "resume.css").read_text(encoding="utf-8")
    assert "LifePath" in html
    assert "/api/chat" in html
    assert "server_llm_key_configured" in html
    assert "waiting_for_profile" in html
    assert "lifepath.conversations.v1" in html
    assert "spinner" in html
    assert "client_state" in html
    assert "recent_messages:item.lastData.recent_messages" in html
    assert "grid-template-rows:auto auto minmax(0,1fr) auto" in html
    assert "max-height:min(58dvh,440px)" in html
    assert "grid-template-rows:auto auto auto" in html
    assert "#roadmap { max-height" not in html
    assert "AI 服务连接中断" in html
    assert "function listText" in html
    assert "article-preview" in html
    assert "article-overlay" in html
    assert "showExpandedArticle" in html
    assert "点击固定" in html
    assert "/api/onboarding" in html
    assert "onboarding-form" in html
    assert "timeline-track" in html
    assert "edit-profile" in html
    assert "localStorage" in html
    assert "close-onboarding" in html
    assert "requestRoadmapEnrichment" in html
    assert "/api/roadmap/enrich" in html
    assert "/api/roadmap/replan" in html
    assert "manual-replan" in html
    assert "requestManualReplan" in html
    assert "/api/conversations" in html
    assert "/api/conversations/import" in html
    assert "conversation-delete" in html
    assert "deleteConversation" in html
    assert "bootstrapConversations" in html
    assert "refreshConversationsOnFocus" in html
    assert "resume-experience-extracted" in html
    assert "提取结果（以下字段可逐项修改）" in html
    assert "重新从已读取文本提取" in html
    assert "function renderMarkdown(text,sources=[])" in html
    assert "function safeHttpUrl(value)" in html
    assert "markdown-table" in html
    assert "node.innerHTML=renderMarkdown(m.text,sources)" in html
    # User/model text is escaped before this local renderer produces its own
    # small tag whitelist, so it is not treated as arbitrary HTML.
    assert "let safe=esc(value);" in html
    assert "/messages/" in html
    assert "deleteChatMessage" in html
    assert "scrollTop" in html
