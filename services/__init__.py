"""SchemeRadar service layer.

Dependency rule (ARCHITECTURE §1.3) — dependencies point strictly downward:

    Client -> API Gateway -> { Retrieval, Scoring, TinyFish Gateway } -> Storage

The Scoring Engine never calls the Client; the Storage layer never calls
TinyFish; the TinyFish Gateway never reads Qdrant directly.
"""
