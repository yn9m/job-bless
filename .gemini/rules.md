# Agent Instructions & Workflow Rules

- **NO REPEATED PROCESS LOOPS**: Never spawn duplicate or looping background processes (`make run-local`, `main.py`, etc.). Never retry spawning processes in loops. Always wait for user confirmation or let a single process finish cleanly.
