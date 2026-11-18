"""Provider response parsing, using response shapes from each provider's docs."""
from prism.providers.adapters import (parse_anthropic, parse_chat, parse_dataforseo, parse_gemini,
                                      parse_responses)


def test_openai_responses_with_web_search():
    data = {"model": "gpt-5.5", "output": [
        {"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "best headless cms retail"}},
        {"type": "message", "content": [{"type": "output_text", "text": "Contentful and Amplience are popular.",
                                          "annotations": [{"type": "url_citation", "url": "https://www.g2.com/x", "title": "G2"},
                                                          {"type": "url_citation", "url": "https://amplience.com/", "title": "Amplience"}]}]}],
        "usage": {"input_tokens": 5200, "output_tokens": 310}}
    r = parse_responses(data)
    assert r.text.startswith("Contentful")
    assert [c.url for c in r.citations] == ["https://www.g2.com/x", "https://amplience.com/"]
    assert r.search_queries == ["best headless cms retail"]
    assert r.search_calls == 1 and r.input_tokens == 5200 and r.output_tokens == 310


def test_xai_top_level_citations_and_perplexity_agent_results():
    r = parse_responses({"output": [{"type": "message", "content": [{"type": "output_text", "text": "Grok says"}]}],
                         "citations": ["https://x.com/a", "https://example.com/b"]})
    assert [c.url for c in r.citations] == ["https://x.com/a", "https://example.com/b"]
    r = parse_responses({"output": [{"type": "search_results", "results": [{"url": "https://reddit.com/r/x", "title": "r"}],
                                     "queries": ["q1"]},
                                    {"type": "message", "content": [{"type": "output_text", "text": "Answer [1]"}]}]})
    assert r.search_calls == 1 and r.citations[0].url == "https://reddit.com/r/x" and r.search_queries == ["q1"]


def test_anthropic_web_search():
    data = {"model": "claude-sonnet-4-6", "content": [
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "email signature software m365"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
            {"type": "web_search_result", "url": "https://www.exclaimer.com/", "title": "Exclaimer"},
            {"type": "web_search_result", "url": "https://www.codetwo.com/", "title": "CodeTwo"}]},
        {"type": "text", "text": "Based on the search results, "},
        {"type": "text", "text": "Exclaimer is a strong choice.", "citations": [
            {"type": "web_search_result_location", "url": "https://www.exclaimer.com/", "title": "Exclaimer", "cited_text": "..."}]}],
        "usage": {"input_tokens": 9000, "output_tokens": 400, "server_tool_use": {"web_search_requests": 1}}}
    r = parse_anthropic(data)
    assert r.text == "Based on the search results, Exclaimer is a strong choice."
    assert r.citations[0].url == "https://www.exclaimer.com/" and r.citations[0].cited_in_text
    assert {c.url for c in r.citations} == {"https://www.exclaimer.com/", "https://www.codetwo.com/"}
    assert r.search_calls == 1 and r.search_queries == ["email signature software m365"]


def test_gemini_grounding_redirect_urls_use_title_domain():
    data = {"candidates": [{"content": {"parts": [{"text": "Unily and Simpplr lead."}]},
                            "groundingMetadata": {"webSearchQueries": ["best intranet", "intranet m365"],
                                                  "groundingChunks": [{"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
                                                                               "title": "unily.com"}}]}}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 80, "thoughtsTokenCount": 20}}
    r = parse_gemini(data)
    assert r.citations[0].url == "https://unily.com/"
    assert r.search_calls == 2 and r.output_tokens == 100


def test_perplexity_sonar_chat():
    data = {"choices": [{"message": {"content": "Grantify helps with Innovate UK [1]."}}],
            "citations": ["https://grantify.io/"],
            "search_results": [{"title": "Grantify", "url": "https://grantify.io/"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 90, "cost": {"total_cost": 0.0123}}}
    r = parse_chat(data, search_builtin=True)
    assert len(r.citations) == 1 and r.extra_cost_usd == 0.0123 and r.search_calls == 1


def test_dataforseo_ai_overview_and_absent():
    data = {"status_code": 20000, "cost": 0.002, "tasks": [{"status_code": 20000, "result": [{"items": [
        {"type": "organic", "url": "https://x.com"},
        {"type": "ai_overview", "markdown": "Amplience and Contentful are leading options.",
         "references": [{"source": "G2", "domain": "g2.com", "url": "https://www.g2.com/cms", "title": "Best CMS"}]}]}]}]}
    r = parse_dataforseo(data, "dataforseo_ai_overview")
    assert "Amplience" in r.text and r.citations[0].url == "https://www.g2.com/cms" and r.extra_cost_usd == 0.002
    none = {"status_code": 20000, "cost": 0.002, "tasks": [{"status_code": 20000, "result": [{"items": [{"type": "organic"}]}]}]}
    r = parse_dataforseo(none, "dataforseo_ai_overview")
    assert r.text == "" and not r.citations
