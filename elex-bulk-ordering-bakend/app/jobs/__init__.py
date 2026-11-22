"""Job / concurrency engine (PLAN.md §7 phase 3).

The engine is deliberately **Redis-free at its core**: ``runner`` + ``tracker``
+ ``tasks`` are plain asyncio and run inside the API process, so the whole
pipeline is runnable and testable with no broker installed. ``arq_worker`` is an
optional adapter for scaling login/coupon work out to separate worker
containers later (PLAN.md §3) — nothing in the core imports it.
"""
