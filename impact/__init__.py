"""Impact explanation package platform.

Three independently running services over one shared SQLite store:
  - ingestd     : accepts upstream detail revision batches, pins the input boundary
  - recomputed  : recomputes affected aggregates for a batch at its pinned boundary
  - packagesvc  : builds / cancels / resumes / serves self-contained explanation packages
"""
