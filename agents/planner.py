"""
Task Planner for ctx-vault Agent Orchestration
Decomposes goals into skill-based subtasks with dependency management.
"""
import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set
from datetime import datetime

from agents import AgentRole, AgentContext, BaseAgent, create_agent


class TaskStatus(Enum):
    """Status of a task in the plan."""
    PENDING = "pending"
    READY = "ready"  # Dependencies satisfied, ready to execute
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskPriority(Enum):
    """Task priority levels."""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class SubTask:
    """A single subtask in the execution plan."""
    id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    role: AgentRole = AgentRole.RESEARCHER
    description: str = ""
    task: str = ""  # The actual task prompt for the agent
    dependencies: List[str] = field(default_factory=list)  # Task IDs this depends on
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.PENDING
    assigned_agent_id: Optional[str] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    estimated_tokens: int = 2000
    max_retries: int = 3
    
    def is_ready(self, completed_tasks: Set[str]) -> bool:
        """Check if all dependencies are completed."""
        return all(dep in completed_tasks for dep in self.dependencies)
    
    def can_execute(self, completed_tasks: Set[str]) -> bool:
        """Check if task can be executed now."""
        return self.status == TaskStatus.PENDING and self.is_ready(completed_tasks)


@dataclass
class ExecutionPlan:
    """Complete execution plan with multiple subtasks."""
    id: str = field(default_factory=lambda: f"plan_{uuid.uuid4().hex[:8]}")
    goal: str = ""
    tasks: List[SubTask] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def get_task(self, task_id: str) -> Optional[SubTask]:
        """Get task by ID."""
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None
    
    def get_ready_tasks(self, completed_tasks: Set[str]) -> List[SubTask]:
        """Get all tasks ready to execute."""
        return [t for t in self.tasks if t.can_execute(completed_tasks)]
    
    def get_running_tasks(self) -> List[SubTask]:
        """Get all currently running tasks."""
        return [t for t in self.tasks if t.status == TaskStatus.RUNNING]
    
    def is_complete(self) -> bool:
        """Check if all tasks are completed."""
        return all(t.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED) for t in self.tasks)
    
    def has_failures(self) -> bool:
        """Check if any tasks failed."""
        return any(t.status == TaskStatus.FAILED for t in self.tasks)


class TaskPlanner:
    """
    Decomposes high-level goals into skill-based subtasks.
    
    Uses the skill registry from ctx-vault to map tasks to appropriate agents.
    """
    
    def __init__(self, ctx_vault_url: str = "http://localhost:8000", api_key: Optional[str] = None):
        self.ctx_vault_url = ctx_vault_url
        self.api_key = api_key
        self._skill_cache: Optional[List[Dict]] = None
    
    async def _get_skills(self) -> List[Dict]:
        """Get available skills from ctx-vault (cached)."""
        if self._skill_cache is None:
            from agents import SkillClient
            async with SkillClient(self.ctx_vault_url, self.api_key) as client:
                self._skill_cache = await client.list_skills()
        return self._skill_cache
    
    def _match_skill_to_role(self, task_description: str) -> AgentRole:
        """Match a task description to the most appropriate agent role."""
        task_lower = task_description.lower()
        
        # Keywords for each role
        role_keywords = {
            AgentRole.RESEARCHER: [
                "research", "find", "search", "gather", "investigate", "explore",
                "look up", "discover", "source", "reference", "literature"
            ],
            AgentRole.ANALYST: [
                "analyze", "compare", "evaluate", "assess", "review", "examine",
                "synthesize", "summarize", "contrast", "critique", "insight"
            ],
            AgentRole.CODER: [
                "code", "implement", "build", "create", "develop", "write",
                "generate", "program", "script", "function", "class", "api"
            ],
            AgentRole.SYNTHESIZER: [
                "synthesize", "integrate", "combine", "merge", "compile",
                "report", "document", "format", "structure", "organize"
            ],
            AgentRole.VERIFIER: [
                "verify", "validate", "test", "check", "audit", "confirm",
                "ensure", "proof", "debug", "fix", "correct"
            ],
            AgentRole.INGESTOR: [
                "ingest", "import", "load", "crawl", "scrape", "download",
                "fetch", "extract", "parse", "index"
            ],
        }
        
        # Score each role
        scores = {}
        for role, keywords in role_keywords.items():
            score = sum(1 for kw in keywords if kw in task_lower)
            scores[role] = score
        
        # Return highest scoring role, default to RESEARCHER
        if scores:
            best_role = max(scores.keys(), key=lambda r: scores[r] or 0)
            if scores[best_role] > 0:
                return best_role
        
        return AgentRole.RESEARCHER
    
    def _estimate_complexity(self, task_description: str) -> int:
        """Estimate token budget needed for a task."""
        # Simple heuristic based on task length and keywords
        base = 2000
        length_factor = len(task_description) // 10
        
        # Complex tasks need more tokens
        complex_keywords = ["comprehensive", "detailed", "full", "complete", "all", "every"]
        complexity_bonus = sum(500 for kw in complex_keywords if kw in task_description.lower())
        
        return min(base + length_factor + complexity_bonus, 8000)
    
    async def create_plan(
        self,
        goal: str,
        context: Optional[AgentContext] = None,
        max_tasks: int = 10,
    ) -> ExecutionPlan:
        """
        Create an execution plan for a high-level goal.
        
        Decomposes the goal into subtasks with appropriate roles and dependencies.
        """
        plan = ExecutionPlan(goal=goal)
        
        # Get available skills for reference
        skills = await self._get_skills()
        skill_types = {s.get("type") for s in skills if s.get("type")}
        
        # Decompose goal into subtasks based on goal type
        subtasks = await self._decompose_goal(goal, skill_types, max_tasks)
        
        # Assign roles and dependencies
        for i, subtask in enumerate(subtasks):
            role = self._match_skill_to_role(subtask["description"])
            estimated_tokens = self._estimate_complexity(subtask["description"])
            
            task = SubTask(
                role=role,
                description=subtask["description"],
                task=subtask.get("task", subtask["description"]),
                dependencies=subtask.get("dependencies", []),
                priority=TaskPriority(subtask.get("priority", 2)),
                estimated_tokens=estimated_tokens,
                metadata=subtask.get("metadata", {}),
            )
            plan.tasks.append(task)
        
        # Auto-resolve dependencies if not specified
        self._resolve_dependencies(plan)
        
        return plan
    
    async def _decompose_goal(
        self,
        goal: str,
        skill_types: Set[str],
        max_tasks: int
    ) -> List[Dict]:
        """
        Decompose a goal into subtasks.
        
        This uses a rule-based approach; in production, you'd use an LLM.
        """
        goal_lower = goal.lower()
        subtasks = []
        
        # Define decomposition patterns
        patterns = {
            # Research-heavy goals
            ("research", "investigate", "explore"): [
                {"description": f"Research background on: {goal}", "priority": 3},
                {"description": f"Find authoritative sources for: {goal}", "priority": 2},
                {"description": f"Gather recent developments on: {goal}", "priority": 2},
                {"description": f"Synthesize findings on: {goal}", "role": "synthesizer", "dependencies": ["0", "1", "2"], "priority": 3},
            ],
            
            # Analysis goals
            ("analyze", "compare", "evaluate"): [
                {"description": f"Research subject matter for: {goal}", "priority": 2},
                {"description": f"Analyze and compare aspects of: {goal}", "role": "analyst", "dependencies": ["0"], "priority": 3},
                {"description": f"Identify gaps and insights for: {goal}", "role": "analyst", "dependencies": ["1"], "priority": 2},
            ],
            
            # Code generation goals
            ("implement", "build", "create", "develop", "code"): [
                {"description": f"Research patterns and best practices for: {goal}", "priority": 2},
                {"description": f"Design architecture for: {goal}", "role": "analyst", "dependencies": ["0"], "priority": 2},
                {"description": f"Implement code for: {goal}", "role": "coder", "dependencies": ["1"], "priority": 3},
                {"description": f"Verify and test implementation for: {goal}", "role": "verifier", "dependencies": ["2"], "priority": 2},
            ],
            
            # Documentation goals
            ("document", "write", "generate docs"): [
                {"description": f"Gather source material for: {goal}", "priority": 2},
                {"description": f"Structure and outline: {goal}", "role": "analyst", "dependencies": ["0"], "priority": 2},
                {"description": f"Write documentation for: {goal}", "role": "synthesizer", "dependencies": ["1"], "priority": 3},
            ],
            
            # Ingestion goals
            ("ingest", "import", "load", "crawl"): [
                {"description": f"Identify and fetch sources for: {goal}", "role": "ingestor", "priority": 3},
                {"description": f"Parse and index content for: {goal}", "role": "ingestor", "dependencies": ["0"], "priority": 2},
                {"description": f"Verify ingestion quality for: {goal}", "role": "verifier", "dependencies": ["1"], "priority": 1},
            ],
        }
        
        # Match goal to pattern
        for keywords, tasks in patterns.items():
            if any(kw in goal_lower for kw in keywords):
                subtasks = tasks[:max_tasks]
                break
        
        # Default decomposition if no pattern matches
        if not subtasks:
            subtasks = [
                {"description": f"Research and gather information for: {goal}", "priority": 2},
                {"description": f"Analyze and structure findings for: {goal}", "role": "analyst", "dependencies": ["0"], "priority": 2},
                {"description": f"Synthesize final output for: {goal}", "role": "synthesizer", "dependencies": ["1"], "priority": 2},
            ]
        
        # Limit to max_tasks
        return subtasks[:max_tasks]
    
    def _resolve_dependencies(self, plan: ExecutionPlan) -> None:
        """Resolve dependency strings to task IDs."""
        task_ids = [t.id for t in plan.tasks]
        
        for i, task in enumerate(plan.tasks):
            resolved_deps = []
            for dep in task.dependencies:
                # Handle numeric indices (0, 1, 2...) referring to task order
                if dep.isdigit():
                    idx = int(dep)
                    if 0 <= idx < len(task_ids):
                        resolved_deps.append(task_ids[idx])
                else:
                    # Handle task IDs directly
                    if dep in task_ids:
                        resolved_deps.append(dep)
            task.dependencies = resolved_deps
    
    def optimize_plan(self, plan: ExecutionPlan, token_budget: int) -> ExecutionPlan:
        """Optimize plan to fit within token budget."""
        total_estimated = sum(t.estimated_tokens for t in plan.tasks)
        
        if total_estimated <= token_budget:
            return plan
        
        # Sort by priority, remove lowest priority tasks
        sorted_tasks = sorted(plan.tasks, key=lambda t: (t.priority.value, t.estimated_tokens))
        
        removed = 0
        for task in sorted_tasks:
            if total_estimated <= token_budget:
                break
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.SKIPPED
                total_estimated -= task.estimated_tokens
                removed += 1
        
        if removed:
            plan.metadata["optimized"] = True
            plan.metadata["tasks_removed"] = removed
        
        return plan


# Convenience function
async def plan_goal(
    goal: str,
    ctx_vault_url: str = "http://localhost:8000",
    api_key: Optional[str] = None,
    max_tasks: int = 10,
) -> ExecutionPlan:
    """Create an execution plan for a goal."""
    planner = TaskPlanner(ctx_vault_url, api_key)
    return await planner.create_plan(goal, max_tasks=max_tasks)