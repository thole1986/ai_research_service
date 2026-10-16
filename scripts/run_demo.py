"""Explicit manual real-provider E2E; never part of automated pytest."""

import argparse
import time
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--real-provider", action="store_true", required=True)
    parser.add_argument("--acknowledge-cost", action="store_true", required=True)
    parser.add_argument("--key", default=None, help="Idempotency key, not an API key")
    args = parser.parse_args()
    key = args.key or f"manual-{uuid4()}"
    payload = {
        "company_name": "Acme CRM",
        "website": "https://example.com",
        "company_description": "Acme provides B2B SaaS CRM software for SMEs in Vietnam.",
        "icp": "B2B SaaS serving SMEs in Vietnam",
    }
    with httpx.Client(base_url=args.url, timeout=15) as client:
        client.get("/ready").raise_for_status()
        response = client.post(
            "/api/v1/research/jobs", json=payload, headers={"Idempotency-Key": key}
        )
        response.raise_for_status()
        job_id = response.json()["job_id"]
        print(f"job_id={job_id}; idempotency_key={key}")
        for _ in range(180):
            response = client.get(f"/api/v1/research/jobs/{job_id}")
            response.raise_for_status()
            state = response.json()
            if state["status"] in ("COMPLETED", "NEEDS_REVIEW", "FAILED"):
                break
            time.sleep(2)
        else:
            raise SystemExit("Polling timeout; inspect checkpoints before attempting any retry.")
        print(f"status={state['status']}; estimated_cost_usd={state['estimated_cost_usd']}")
        if state["status"] != "COMPLETED":
            raise SystemExit(f"Stopped: {state['error']}; inspect events before retry.")
        attempts = [step["attempt_count"] for step in state["steps"]]
        duplicate = client.post(
            "/api/v1/research/jobs", json=payload, headers={"Idempotency-Key": key}
        )
        assert duplicate.status_code == 200 and duplicate.json()["job_id"] == job_id
        after = client.get(f"/api/v1/research/jobs/{job_id}").json()
        assert [step["attempt_count"] for step in after["steps"]] == attempts
        print("PASS: completed four steps; duplicate preserved job ID and attempt counts.")
        print(state["result"]["report"]["executive_summary"])


if __name__ == "__main__":
    main()
