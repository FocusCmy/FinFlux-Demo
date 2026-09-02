---
name: guard-execution-budget
description: Verify run-scoped wall-time, tool-timeout, retry and checkpoint limits before bounded Agent execution.
---

# Guard Execution Budget

Require the pinned bounded execution policy, a tool timeout no greater than 90 seconds, zero implicit retries and a durable checkpoint requirement. Return `READY` or `HOLD`. Provider token hard-cap availability must be reported truthfully and never inferred from a Matrix character estimate.
