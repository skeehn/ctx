"""
Orchestrator for ctx-vault Agent Orchestration
Coordinates sub-agents, handles failures/retries, manages execution flow.
"""
import asyncio
import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Callable, Awaitable
from collections import defaultdict

from agents import (
    AgentRole, AgentStatus, AgentContext, AgentResult, BaseAgent,
    TokenBudget, create_agent
)
from agents.planner import TaskPlanner, ExecutionPlan, SubTask, TaskStatus, TaskPriority
from agents.router import SkillRouter, SkillRegistry, create_skill_router
from agents.context import ContextManager, ContextWindow, create_context_manager


class OrchestrationStrategy(Enum):
    """Strategy for executing a plan."""
    SEQUENTIAL = "sequential"  # Execute tasks one at a time
    PARALLEL = "parallel"      # Execute ready tasks in parallel
    PIPELINE = "pipeline"      # Pipeline: start next when previous produces output
    ADAPTIVE = "adaptive"      # Dynamically choose based on dependencies


class OrchestratorStatus(Enum):
    """Orchestrator execution status."""
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass
class OrchestrationResult:
    """Result of an orchestration run."""
    orchestration_id: str
    goal: str
    plan: ExecutionPlan
    status: OrchestratorStatus
    final_output: Any = None
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    total_tokens_used: int = 0
    task_results: Dict[str, AgentResult] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def duration_ms(self) -> int:
        end = self.completed_at or time.time()
        return int((end - self.started_at) * 1000)


class Orchestrator:
    """
    Main orchestrator that coordinates multi-agent task execution.
    
    Features:
    - Goal decomposition via TaskPlanner
    - Skill routing via SkillRouter
    - Context management via ContextManager
    - Parallel/sequential execution with dependency resolution
    - Retry logic with exponential backoff
    - Token budget management across agents
    - Checkpointing and recovery
    - Progress callbacks
    """
    
    def __init__(
        self,
        ctx_vault_url: str = "http://localhost:8000",
        vector_url: Optional[str] = None,
        api_key: Optional[str] = None,
        default_token_budget: int = 32000,
        max_concurrent_tasks: int = 5,
        default_strategy: OrchestrationStrategy = OrchestrationStrategy.ADAPTIVE,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        self.ctx_vault_url = ctx_vault_url
        self.api_key = api_key
        self.default_token_budget = default_token_budget
        self.max_concurrent_tasks = max_concurrent_tasks
        self.default_strategy = default_strategy
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        # Core components
        self.task_planner = TaskPlanner(ctx_vault_url, api_key)
        self.skill_router: Optional[SkillRouter] = None
        self.context_manager: Optional[ContextManager] = None
        self.skill_registry: Optional[SkillRegistry] = None
        
        # State
        self.status = OrchestratorStatus.IDLE
        self.current_orchestration: Optional[OrchestrationResult] = None
        self._progress_callbacks: List[Callable[[OrchestrationResult], Awaitable[None]]] = []
        self._agent_pool: Dict[str, BaseAgent] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None
    
    async def initialize(self) -> None:
        """Initialize all components."""
        self.skill_registry = SkillRegistry(self.ctx_vault_url, self.api_key)
        await self.skill_registry.sync_from_vault()
        
        self.skill_router = SkillRouter(self.skill_registry)
        
        self.context_manager = await create_context_manager(
            vault_url=self.ctx_vault_url,
            vector_url=None,  # Will be configured when cilow is available
            api_key=self.api_key,
        )
        
        self._semaphore = asyncio.Semaphore(self.max_concurrent_tasks)
    
    def add_progress_callback(self, callback: Callable[[OrchestrationResult], Awaitable[None]]) -> None:
        """Add a progress callback."""
        self._progress_callbacks.append(callback)
    
    async def _notify_progress(self, result: OrchestrationResult) -> None:
        """Notify all progress callbacks."""
        for callback in self._progress_callbacks:
            try:
                await callback(result)
            except Exception:
                pass  # Don't let callback errors break orchestration
    
    async def execute_goal(
        self,
        goal: str,
        strategy: Optional[OrchestrationStrategy] = None,
        context: Optional[AgentContext] = None,
        max_tasks: int = 10,
        token_budget: Optional[int] = None,
    ) -> OrchestrationResult:
        """
        Execute a high-level goal by decomposing, planning, and running agents.
        
        This is the main entry point for orchestration.
        """
        if self.status == OrchestratorStatus.EXECUTING:
            raise RuntimeError("Orchestrator already executing a goal")
        
        strategy = strategy or self.default_strategy
        token_budget = token_budget or self.default_token_budget
        
        # Create orchestration result
        orchestration = OrchestrationResult(
            orchestration_id=f"orch_{uuid.uuid4().hex[:8]}",
            goal=goal,
            plan=ExecutionPlan(goal=goal),
            status=OrchestratorStatus.PLANNING,
        )
        
        self.status = OrchestratorStatus.PLANNING
        self.current_orchestration = orchestration
        await self._notify_progress(orchestration)
        
        try:
            # Phase 1: Create execution plan
            plan = await self.task_planner.create_plan(
                goal=goal,
                context=context,
                max_tasks=max_tasks,
            )
            
            # Optimize plan for token budget
            plan = self.task_planner.optimize_plan(plan, token_budget)
            
            orchestration.plan = plan
            orchestration.status = OrchestratorStatus.EXECUTING
            await self._notify_progress(orchestration)
            
            # Phase 2: Execute plan
            result = await self._execute_plan(
                plan=plan,
                strategy=strategy,
                token_budget=token_budget,
                root_context=context,
            )
            
            orchestration.final_output = result.final_output
            orchestration.task_results = result.task_results
            orchestration.total_tokens_used = result.total_tokens_used
            orchestration.errors = result.errors
            orchestration.status = OrchestratorStatus.COMPLETED if not result.errors else OrchestratorStatus.FAILED
            orchestration.completed_at = time.time()
            
        except Exception as e:
            orchestration.status = OrchestratorStatus.FAILED
            orchestration.errors.append(str(e))
            orchestration.completed_at = time.time()
        
        self.status = OrchestratorStatus.IDLE
        self.current_orchestration = None
        await self._notify_progress(orchestration)
        
        return orchestration
    
    async def _execute_plan(
        self,
        plan: ExecutionPlan,
        strategy: OrchestrationStrategy,
        token_budget: int,
        root_context: Optional[AgentContext],
    ) -> OrchestrationResult:
        """Execute a plan using the specified strategy."""
        
        # Initialize token budget
        global_budget = TokenBudget(total=token_budget)
        
        # Track completed tasks
        completed_tasks: Set[str] = set()
        task_results: Dict[str, AgentResult] = {}
        errors: List[str] = []
        
        # Create root agent context
        if root_context is None:
            root_context = AgentContext(
                agent_id=f"root_{uuid.uuid4().hex[:8]}",
                role=AgentRole.ROOT,
                task=plan.goal,
                token_budget=global_budget,
            )
        
        # Execution loop
        while not plan.is_complete():
            # Check for failures
            if plan.has_failures() and strategy != OrchestrationStrategy.SEQUENTIAL:
                # In non-sequential mode, fail fast on any failure
                failed = [t for t in plan.tasks if t.status == TaskStatus.FAILED]
                for f in failed:
                    errors.append(f"Task {f.id} failed: {f.error}")
                break
            
            # Get ready tasks
            ready_tasks = plan.get_ready_tasks(completed_tasks)
            
            if not ready_tasks:
                # No ready tasks but plan not complete - deadlock or all running
                running = plan.get_running_tasks()
                if not running:
                    errors.append("Deadlock: no ready tasks but plan not complete")
                    break
                # Wait for running tasks
                await asyncio.sleep(0.1)
                continue
            
            # Execute ready tasks based on strategy
            if strategy == OrchestrationStrategy.SEQUENTIAL:
                # Execute one at a time
                for task in ready_tasks[:1]:
                    await self._execute_task(
                        task, plan, root_context, global_budget,
                        task_results, completed_tasks, errors
                    )
            elif strategy == OrchestrationStrategy.PARALLEL:
                # Execute all ready tasks in parallel (limited by semaphore)
                await self._execute_parallel(
                    ready_tasks, plan, root_context, global_budget,
                    task_results, completed_tasks, errors
                )
            elif strategy == OrchestrationStrategy.PIPELINE:
                # Execute with pipelining
                await self._execute_pipeline(
                    ready_tasks, plan, root_context, global_budget,
                    task_results, completed_tasks, errors
                )
            else:  # ADAPTIVE
                # Choose based on task types
                await self._execute_adaptive(
                    ready_tasks, plan, root_context, global_budget,
                    task_results, completed_tasks, errors
                )
            
            await self._notify_progress(OrchestrationResult(
                orchestration_id="",
                goal=plan.goal,
                plan=plan,
                status=OrchestratorStatus.EXECUTING,
                task_results=task_results,
                errors=errors,
            ))
        
        # Synthesize final output
        final_output = await self._synthesize_output(plan, task_results, root_context)
        
        return OrchestrationResult(
            orchestration_id="",
            goal=plan.goal,
            plan=plan,
            status=OrchestratorStatus.COMPLETED if not errors else OrchestratorStatus.FAILED,
            final_output=final_output,
            task_results=task_results,
            total_tokens_used=sum(r.tokens_used for r in task_results.values()),
            errors=errors,
        )
    
    async def _execute_task(
        self,
        task: SubTask,
        plan: ExecutionPlan,
        root_context: AgentContext,
        global_budget: TokenBudget,
        task_results: Dict[str, AgentResult],
        completed_tasks: Set[str],
        errors: List[str],
    ) -> None:
        """Execute a single task."""
        task.status = TaskStatus.RUNNING
        
        # Create agent for this task
        agent = create_agent(
            task.role,
            ctx_vault_url=self.ctx_vault_url,
            api_key=self.api_key,
            token_budget=task.estimated_tokens,
            max_retries=task.max_retries,
            retry_delay=self.retry_delay,
        )
        
        # Build agent context with dependencies
        dep_results = {}
        for dep_id in task.dependencies:
            dep_result = task_results.get(dep_id)
            if dep_result and dep_result.output:
                dep_results[dep_id] = dep_result.output
        
        agent_context = AgentContext(
            agent_id=f"{task.role.value}_{task.id}",
            role=task.role,
            task=task.task,
            parent_id=root_context.agent_id,
            token_budget=TokenBudget(total=task.estimated_tokens),
            metadata={
                **task.metadata,
                "dependencies": dep_results,
                "plan_id": plan.id,
                "task_id": task.id,
            },
            skills=task.metadata.get("skills", []),
        )
        
        # Reserve tokens from global budget
        if not global_budget.reserve(task.estimated_tokens):
            task.status = TaskStatus.FAILED
            task.error = "Insufficient global token budget"
            errors.append(f"Task {task.id}: {task.error}")
            return
        
        try:
            # Build context for this task
            context_window = None
            if self.context_manager:
                context_window = await self.context_manager.create_window(
                    max_tokens=task.estimated_tokens,
                )
                await self.context_manager.build_context(
                    query=task.task,
                    window=context_window,
                    agent_id=agent_context.agent_id,
                )
            
            # Add context to agent metadata
            if context_window:
                agent_context.metadata["context"] = context_window.get_context_string()
            
            # Execute agent
            result = await agent.run(task.task, parent_context=agent_context)
            
            # Commit tokens used
            global_budget.commit(result.tokens_used)
            
            # Store result
            task.result = result.output
            task.status = TaskStatus.COMPLETED if result.status == AgentStatus.COMPLETED else TaskStatus.FAILED
            task.error = result.error
            task_results[task.id] = result
            
            if task.status == TaskStatus.COMPLETED:
                completed_tasks.add(task.id)
            else:
                errors.append(f"Task {task.id} ({task.role.value}): {result.error}")
                
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            errors.append(f"Task {task.id} exception: {e}")
            global_budget.release(task.estimated_tokens)
    
    async def _execute_parallel(
        self,
        ready_tasks: List[SubTask],
        plan: ExecutionPlan,
        root_context: AgentContext,
        global_budget: TokenBudget,
        task_results: Dict[str, AgentResult],
        completed_tasks: Set[str],
        errors: List[str],
    ) -> None:
        """Execute multiple tasks in parallel."""
        if not self._semaphore:
            self._semaphore = asyncio.Semaphore(self.max_concurrent_tasks)
            
        async def execute_one(task: SubTask):
            async with self._semaphore:
                await self._execute_task(
                    task, plan, root_context, global_budget,
                    task_results, completed_tasks, errors
                )
        
        # Launch all ready tasks
        await asyncio.gather(*[execute_one(t) for t in ready_tasks])
    
    async def _execute_pipeline(
        self,
        ready_tasks: List[SubTask],
        plan: ExecutionPlan,
        root_context: AgentContext,
        global_budget: TokenBudget,
        task_results: Dict[str, AgentResult],
        completed_tasks: Set[str],
        errors: List[str],
    ) -> None:
        """Execute tasks in pipeline mode (start next when previous produces output)."""
        # For now, same as parallel but with dependency on output
        await self._execute_parallel(
            ready_tasks, plan, root_context, global_budget,
            task_results, completed_tasks, errors
        )
    
    async def _execute_adaptive(
        self,
        ready_tasks: List[SubTask],
        plan: ExecutionPlan,
        root_context: AgentContext,
        global_budget: TokenBudget,
        task_results: Dict[str, AgentResult],
        completed_tasks: Set[str],
        errors: List[str],
    ) -> None:
        """Adaptively choose execution strategy based on task characteristics."""
        # Group tasks by role
        by_role = defaultdict(list)
        for task in ready_tasks:
            by_role[task.role].append(task)
        
        # Execute each role group in parallel, roles in sequence
        for role, tasks in by_role.items():
            if len(tasks) == 1:
                await self._execute_task(
                    tasks[0], plan, root_context, global_budget,
                    task_results, completed_tasks, errors
                )
            else:
                await self._execute_parallel(
                    tasks, plan, root_context, global_budget,
                    task_results, completed_tasks, errors
                )
    
    async def _synthesize_output(
        self,
        plan: ExecutionPlan,
        task_results: Dict[str, AgentResult],
        root_context: AgentContext,
    ) -> Any:
        """Synthesize final output from all task results."""
        # Use a synthesizer agent to combine results
        synthesizer = create_agent(
            AgentRole.SYNTHESIZER,
            ctx_vault_url=self.ctx_vault_url,
            api_key=self.api_key,
            token_budget=4000,
        )
        
        # Prepare synthesis context
        synthesis_context = AgentContext(
            agent_id=f"synthesizer_{uuid.uuid4().hex[:8]}",
            role=AgentRole.SYNTHESIZER,
            task=f"Synthesize final output for goal: {plan.goal}",
            parent_id=root_context.agent_id,
            token_budget=TokenBudget(total=4000),
            metadata={
                "goal": plan.goal,
                "task_results": {tid: r.output for tid, r in task_results.items() if r.output},
                "plan_tasks": [{"id": t.id, "role": t.role.value, "description": t.description} for t in plan.tasks],
            },
        )
        
        synthesis_result = await synthesizer.run(
            f"Synthesize a comprehensive final output for the goal: {plan.goal}",
            parent_context=synthesis_context,
        )
        
        return synthesis_result.output if synthesis_result.output else {
            "goal": plan.goal,
            "task_results": {tid: r.output for tid, r in task_results.items() if r.output},
            "summary": f"Completed {len([r for r in task_results.values() if r.status == AgentStatus.COMPLETED])}/{len(task_results)} tasks",
        }
    
    async def execute_with_recovery(
        self,
        goal: str,
        checkpoint_interval: int = 5,
        **kwargs
    ) -> OrchestrationResult:
        """
        Execute a goal with checkpointing for recovery.
        
        Saves progress every N tasks to allow recovery from failures.
        """
        # This would implement checkpointing - for now just execute normally
        return await self.execute_goal(goal, **kwargs)
    
    def get_status(self) -> Dict[str, Any]:
        """Get current orchestrator status."""
        return {
            "status": self.status.value,
            "current_orchestration": self.current_orchestration.orchestration_id if self.current_orchestration else None,
            "agent_pool_size": len(self._agent_pool),
        }
    
    async def shutdown(self) -> None:
        """Shutdown orchestrator and cleanup resources."""
        self.status = OrchestratorStatus.IDLE
        self.current_orchestration = None
        self._agent_pool.clear()


# Convenience function
async def orchestrate_goal(
    goal: str,
    ctx_vault_url: str = "http://localhost:8000",
    api_key: Optional[str] = None,
    **kwargs
) -> OrchestrationResult:
    """Convenience function to orchestrate a goal."""
    orchestrator = Orchestrator(ctx_vault_url=ctx_vault_url, api_key=api_key)
    await orchestrator.initialize()
    try:
        return await orchestrator.execute_goal(goal, **kwargs)
    finally:
        await orchestrator.shutdown()