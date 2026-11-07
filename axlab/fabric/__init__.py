"""Slim agent fabric: tens of thousands of agents as packed, signed records,
a few model slots, one shared model.

  slim_pool        SlimAgentPool, AgentTemplate, AgentState (routing + rows)
  embedding_board  EmbeddingBoard, hashed_embed, OllamaEmbedder
  agent_store      ContinuationStore, AgentMessage (suspended state, inboxes)
  runtime          SlimFabricRuntime (model slots, tools, fleet kill switch)
  backends         BackendResult, OllamaBackend

Import the submodule you need; this package imports nothing on its own.
"""
