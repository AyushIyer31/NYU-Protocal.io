# Saturday Demo Notes — Protocols.io Chatbot

## One-sentence summary

This week I improved the protocols.io chatbot by adding a **clarification layer** and **multi-query generation** on top of the existing TF-IDF search, so that vague biology protocol requests are handled gracefully and the search covers more relevant protocols even when the user phrases things differently.

---

## What was built this week

### 1. Clarification layer (`protocol_rag.py` — `is_vague_query`, `get_clarification_question`)

Before searching, the system now checks whether the query is too vague to produce good results. Short, generic queries like "PCR", "overexpression", or "I want to test gene expression" are flagged. Instead of returning weak results, the chatbot asks one targeted follow-up question.

**Demo step:**
> User: "I want to test overexpression"
> Assistant: "Do you mean gene overexpression, protein overexpression, or overexpression in a specific system (e.g. mammalian cells, plants, bacteria)?"

### 2. Multi-query generation (`protocol_rag.py` — `expand_query`)

After receiving a valid query, the system generates 3–5 related search variants using synonym substitution (e.g. extraction → isolation → purification). When Ollama is running, the local LLM is used for richer rephrasing.

**Demo step:**
> User: "gene overexpression in cell culture"
> System generates: ["gene overexpression in cell culture", "gene expression in cell culture", "gene regulation in cell culture protocol", "gene overexpression in cell culture method"]

### 3. Multi-query search and re-ranking (`protocol_rag.py` — `multi_search_protocols`)

All generated query variants are searched against the TF-IDF index. Results are merged, deduplicated by protocol ID, and re-ranked using:

```
combined_score = best_tfidf_score + 0.05 × (number of query variants matched − 1)
```

Protocols that surface across multiple queries get a small frequency bonus — they are more likely to be broadly relevant.

### 4. Feedback buttons (`chat.html`)

After search results are shown, the chatbot asks: *"Were these results relevant? I can help narrow the search."*

Four buttons let the user refine the query:
- **Narrow by organism** → pre-fills input with `[query] in `
- **Narrow by technique** → pre-fills with `[query] using `
- **Narrow by sample type** → pre-fills with `[query] from `
- **Narrow by experimental goal** → pre-fills with `[query] for `

The user completes the sentence and submits a more specific query.

---

## Full demo flow to show in the meeting

```
1. Open http://localhost:5555/chat.html

2. Type:  "I want to test overexpression"
   → Chatbot asks a clarification question (yellow bubble)

3. Type:  "gene overexpression in cell culture"
   → System shows "Also searched:" tag row with all expanded queries
   → Returns ranked protocol cards with match count badges
   → Shows feedback buttons at the bottom

4. Click "Narrow by organism"
   → Input pre-fills with "gene overexpression in cell culture in "
   → Type "mammalian cells" and press Enter
   → New, narrower results returned
```

---

## How to run locally

```bash
python3 run.py
# Backend: http://localhost:8001
# Chat UI: http://localhost:5555/chat.html
```

Ollama is optional. If not installed, search works fully — LLM explanations are just skipped.

---

## Known limitations (honest framing for the meeting)

| Limitation | Why it matters | Future fix |
|---|---|---|
| TF-IDF is keyword-based | "isolate mRNA" won't match "RNA extraction" well | Sentence-transformer embeddings |
| Only 962 protocols indexed | protocols.io has 100k+; coverage is narrow | Broader keyword crawl or full API crawl |
| No conversation memory | Can't say "show more like result #2" | Add session state / chat history |
| Clarification is rule-based | Won't catch every vague query | LLM-based vague detection |
| Feedback buttons don't learn | Clicks don't improve future rankings | Collect signal → fine-tune ranking |
| Ollama required for explanations | No LLM = no plain-English explanation | Add hosted model option |

---

## Files changed

| File | What changed |
|---|---|
| `protocolsnerd-backend/protocol_rag.py` | Added `is_vague_query`, `get_clarification_question`, `expand_query`, `multi_search_protocols` |
| `protocolsnerd-backend/main.py` | Updated `/chat` endpoint to use new functions; added `skip_clarification` field |
| `protocolsnerd-website/chat.html` | Added clarification bubble, expanded-queries tags, feedback buttons |
| `README.md` | Added "Protocols.io Chatbot" section explaining the system |
| `SATURDAY_DEMO_NOTES.md` | This file |
