# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
import json
from genlayer import *

class FactChecker(gl.Contract):

    claims: DynArray[str]

    def __init__(self):
        pass

    @gl.public.write
    def submit_claim(self, claim: str, url: str) -> None:
        assert len(claim) > 0, "Empty claim"
        assert len(url) > 0, "Empty url"
        entry = {
            "id": len(self.claims),
            "submitter": str(gl.message.sender_address),
            "claim": claim,
            "url": url,
            "status": "PENDING",
            "verdict": "",
            "reasoning": "",
        }
        self.claims.append(json.dumps(entry, sort_keys=True))

    @gl.public.write
    def verify_claim(self, claim_id: int) -> None:
        entry = json.loads(self.claims[claim_id])
        assert entry["status"] == "PENDING", "Already verified"
        claim_text = entry["claim"]
        url = entry["url"]

        def run() -> str:
            page = gl.nondet.web.render(url, mode="text")[:5000]
            p = (
                "You are a fact-checker.\n"
                "CLAIM: " + claim_text + "\n"
                "EVIDENCE from " + url + ":\n" + page + "\n\n"
                "Does the evidence support the claim?\n"
                "Reply with JSON only: {\"verdict\": \"TRUE\" or \"FALSE\", \"reasoning\": \"one sentence\"}"
            )
            r = gl.nondet.exec_prompt(p)
            r = r.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(r)
            return json.dumps({
                "verdict": parsed.get("verdict", "FALSE"),
                "reasoning": parsed.get("reasoning", ""),
            }, sort_keys=True)

        consensus = gl.eq_principle.prompt_comparative(
            run,
            principle="The verdict field must be exactly the same.",
        )
        result = json.loads(consensus)
        entry["status"] = "VERIFIED"
        entry["verdict"] = result.get("verdict", "FALSE")
        entry["reasoning"] = result.get("reasoning", "")
        self.claims[claim_id] = json.dumps(entry, sort_keys=True)

    @gl.public.view
    def get_claim(self, claim_id: int) -> str:
        return self.claims[claim_id]

    @gl.public.view
    def get_all_claims(self) -> list[str]:
        return list(self.claims)

    @gl.public.view
    def total_claims(self) -> int:
        return len(self.claims)
