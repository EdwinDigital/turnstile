"""HTTP-facing business rules: budgets, model access, and Assistant orchestration.

This is where a decision gets made rather than stored or transported. Services raise
`HTTPException` for outcomes the caller must see, which keeps the route layer thin at the
cost of a FastAPI import here.

May import any layer in `turnstile_core`. Must not import `api` or `http`.
"""
