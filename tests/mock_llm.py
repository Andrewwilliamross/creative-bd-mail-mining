#!/usr/bin/env python3
"""Tiny mock of the ScaleDown (classify / extract / summarize / compress) and OpenAI-compatible endpoints, built from the response shapes in
https://docs.scaledown.ai/api-reference/{classify,extract,summarize,compress}. It only proves dcrmine's request/response handling and
anti-hallucination guards; it says NOTHING about real model quality. Usage: python3 mock_llm.py PORT"""
import json, re, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
SEEN = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, o, code=200): b = json.dumps(o).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); p = self.path; key = self.headers.get("x-api-key") or self.headers.get("Authorization")
        SEEN.append((p, key, body.get("model")))
        if p.startswith("/classify"):
            assert key == "sd-test-key"; names = [l["name"] for l in body["labels"]]; res = []
            for ch in body["chunks"]:
                t = ch["text"].lower()
                if "project_work" in names: top = "project_work" if re.search(r"trophy|conference room|airport|set build|backdrop|quote", t) else "other_business"
                else: top = "paid" if "payment sent" in t or "paid" in t else "invoiced" if "invoice" in t else "bid" if "quote" in t else "inquiry"
                sc = {n: (0.9 if n == top else 0.1 / max(1, len(names) - 1)) for n in names}; res.append({"id": ch["id"], "top_label": top, "matched_labels": None, "scores": sc, "labels": []})
            return self._send({"top_label": res[0]["top_label"], "scores": res[0]["scores"], "labels": [], "results": res, "chunk_count": len(res)})
        if p.startswith("/compress/raw"):
            assert key == "sd-test-key"; return self._send({"success": True, "results": {"success": True, "compressed_prompt": body["context"][:6000], "original_prompt": body["prompt"]}})
        if p.startswith("/v1/chat/completions") or p.startswith("/chat/completions"):
            m = body.get("model")
            if m == "extract":
                text = body["messages"][-1]["content"]; ents = []
                for typ, rx in (("client_company", r"Pepsi|Paramount|NOBL|Fieldhouse"), ("money_amount", r"\$[\d,]+"), ("person", r"Maria Lopez|Charlie Ward|Jordan Blake"), ("deliverable", r"trophy|backdrop|conference room"), ("project_name", r"Rookie of the Year trophy|conference room")):
                    for mm in re.finditer(rx, text): ents.append({"text": mm.group(0), "type": typ, "confidence": 0.9, "start": mm.start(), "end": mm.end(), "context": ""})
                ents.append({"text": "Hallucinated Corp", "type": "client_company", "confidence": 0.99, "start": 0, "end": 0, "context": ""})  # must be rejected by the grounding guard
                return self._send({"model": "extract", "choices": [{"message": {"content": json.dumps({"entities": ents, "structured_result": None, "input_tokens": 10})}}]})
            if m == "summarize": return self._send({"model": "summarize", "choices": [{"message": {"content": json.dumps({"summary": "Mock summary: client asked for a set, David quoted, built and delivered it, and was paid.", "finish_reason": "stop"})}}]})
            out = {"kind": "project_work", "is_project": True, "client": "Totally Invented Brand", "project_name": "Rookie of the Year trophy", "status": "paid", "deliverables": ["trophy", "unicorn statue"], "money": [{"kind": "quote", "amount": 999999, "date": "2020-01-01", "evidence": "fabricated"}], "summary": "OpenAI-mock summary of the thread, three sentences long."}
            return self._send({"choices": [{"message": {"content": json.dumps(out)}}]})
        self._send({"error": "nf"}, 404)
if __name__ == "__main__": HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
