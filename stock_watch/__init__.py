# -*- coding: utf-8 -*-
"""Stock news watchers that hand fresh headlines to other agents.

Kept outside ``agent_reach`` on purpose: Agent Reach is an installer/doctor
glue layer synced from upstream, while these watchers are long-running
personal monitors built on top of it.
"""
