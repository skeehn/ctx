"""
Skill Router for ctx-vault Agent Orchestration
Maps tasks to skills and manages skill discovery, selection, and execution.
"""
import asyncio
import hashlib
import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Awaitable

from agents import AgentRole, BaseAgent, SkillClient, AgentContext, AgentResult, AgentStatus
from agents.context import ContextManager


class SkillType(Enum):
    """Types of skills available in the system."""
    INGESTION = "ingestion"
    SEARCH = "search"
    ANALYSIS = "analysis"
    CODE = "code"
    SYNTHESIS = "synthesis"
    VERIFICATION = "verification"
    TRANSFORMATION = "transformation"
    EXTERNAL = "external"  # Web search, API calls, etc.


class SkillStatus(Enum):
    """Status of a skill."""
    AVAILABLE = "available"
    BUSY = "busy"
    ERROR = "error"
    DEPRECATED = "deprecated"


@dataclass
class Skill:
    """Represents a skill in the registry."""
    id: str
    name: str
    type: SkillType
    description: str
    input_schema: Dict[str, Any]  # JSON schema for input
    output_schema: Dict[str, Any]  # JSON schema for output
    handler: Optional[Callable] = None  # Local handler function
    endpoint: Optional[str] = None  # Remote endpoint URL
    estimated_tokens: int = 1000
    max_concurrent: int = 5
    tags: List[str] = field(default_factory=list)
    version: str = "1.0.0"
    status: SkillStatus = SkillStatus.AVAILABLE
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def matches_task(self, task_description: str) -> float:
        """Calculate match score for a task (0-1)."""
        task_lower = task_description.lower()
        
        # Check tags
        tag_matches = sum(1 for tag in self.tags if tag.lower() in task_lower)
        
        # Check description
        desc_words = set(self.description.lower().split())
        task_words = set(task_lower.split())
        desc_matches = len(desc_words & task_words) / max(len(desc_words), 1)
        
        # Check name
        name_match = 1.0 if self.name.lower() in task_lower else 0.0
        
        # Weighted score
        score = (tag_matches * 0.4 + desc_matches * 0.4 + name_match * 0.2)
        return min(score, 1.0)


@dataclass
class SkillExecution:
    """Tracks a skill execution."""
    id: str = field(default_factory=lambda: f"exec_{uuid.uuid4().hex[:8]}")
    skill_id: str = ""
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Optional[Dict[str, Any]] = None
    status: SkillStatus = SkillStatus.AVAILABLE
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    tokens_used: int = 0
    error: Optional[str] = None
    agent_id: Optional[str] = None


class SkillRegistry:
    """
    Registry of available skills with discovery and matching capabilities.
    
    Integrates with ctx-vault skill API for persistent skill storage.
    """
    
    def __init__(self, ctx_vault_url: str = "http://localhost:8000", api_key: Optional[str] = None):
        self.ctx_vault_url = ctx_vault_url
        self.api_key = api_key
        self._skills: Dict[str, Skill] = {}
        self._executions: Dict[str, SkillExecution] = {}
        self._skill_client: Optional[SkillClient] = None
        self._local_skills: Dict[str, Skill] = {}
    
    @property
    def skill_client(self) -> SkillClient:
        if self._skill_client is None:
            self._skill_client = SkillClient(self.ctx_vault_url, self.api_key)
        return self._skill_client
    
    def register_local_skill(self, skill: Skill) -> None:
        """Register a locally implemented skill."""
        self._local_skills[skill.id] = skill
        self._skills[skill.id] = skill
    
    async def sync_from_vault(self) -> int:
        """Sync skills from ctx-vault."""
        try:
            async with self.skill_client as client:
                vault_skills = await client.list_skills()
            
            count = 0
            for vs in vault_skills:
                skill = self._parse_vault_skill(vs)
                if skill:
                    self._skills[skill.id] = skill
                    count += 1
            
            return count
        except Exception:
            return 0
    
    def _parse_vault_skill(self, vs: Dict) -> Optional[Skill]:
        """Parse a skill from vault format."""
        try:
            skill_type = SkillType(vs.get("type", "external"))
        except ValueError:
            skill_type = SkillType.EXTERNAL
        
        return Skill(
            id=vs.get("id", f"skill_{hashlib.md5(vs.get('name', '').encode()).hexdigest()[:8]}"),
            name=vs.get("name", "Unknown Skill"),
            type=skill_type,
            description=vs.get("description", ""),
            input_schema=vs.get("input_schema", {"type": "object"}),
            output_schema=vs.get("output_schema", {"type": "object"}),
            endpoint=vs.get("endpoint"),
            estimated_tokens=vs.get("estimated_tokens", 1000),
            max_concurrent=vs.get("max_concurrent", 5),
            tags=vs.get("tags", []),
            version=vs.get("version", "1.0.0"),
            status=SkillStatus(vs.get("status", "available")),
            metadata=vs.get("metadata", {}),
        )
    
    def get_skill(self, skill_id: str) -> Optional[Skill]:
        """Get a skill by ID."""
        return self._skills.get(skill_id)
    
    def find_skills(
        self,
        task_description: str,
        skill_type: Optional[SkillType] = None,
        min_score: float = 0.1,
        max_results: int = 10,
    ) -> List[Tuple[Skill, float]]:
        """Find skills matching a task description."""
        matches = []
        
        for skill in self._skills.values():
            if skill.status != SkillStatus.AVAILABLE:
                continue
            
            if skill_type and skill.type != skill_type:
                continue
            
            score = skill.matches_task(task_description)
            if score >= min_score:
                matches.append((skill, score))
        
        # Sort by score descending
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches[:max_results]
    
    def get_skills_by_type(self, skill_type: SkillType) -> List[Skill]:
        """Get all skills of a specific type."""
        return [s for s in self._skills.values() if s.type == skill_type and s.status == SkillStatus.AVAILABLE]
    
    async def execute_skill(
        self,
        skill_id: str,
        input_data: Dict[str, Any],
        agent_id: Optional[str] = None,
    ) -> SkillExecution:
        """Execute a skill with given input."""
        skill = self.get_skill(skill_id)
        if not skill:
            return SkillExecution(
                skill_id=skill_id,
                input_data=input_data,
                status=SkillStatus.ERROR,
                error=f"Skill not found: {skill_id}",
                agent_id=agent_id,
            )
        
        execution = SkillExecution(
            skill_id=skill_id,
            input_data=input_data,
            agent_id=agent_id,
        )
        self._executions[execution.id] = execution
        
        try:
            if skill.handler:
                # Local handler
                if asyncio.iscoroutinefunction(skill.handler):
                    output = await skill.handler(input_data)
                else:
                    output = skill.handler(input_data)
            elif skill.endpoint:
                # Remote endpoint
                output = await self._call_remote_endpoint(skill, input_data)
            else:
                # Default: simulate skill execution (would call actual skill API in production)
                output = {"simulated": True, "skill_id": skill_id, "input": input_data}
            
            execution.output_data = output
            execution.status = SkillStatus.AVAILABLE
            execution.completed_at = time.time()
            execution.tokens_used = len(json.dumps(output)) // 4
            
        except Exception as e:
            execution.status = SkillStatus.ERROR
            execution.error = str(e)
            execution.completed_at = time.time()
        
        return execution
    
    async def _call_remote_endpoint(self, skill: Skill, input_data: Dict) -> Dict:
        """Call a remote skill endpoint."""
        if not skill.endpoint:
            raise ValueError("Skill has no endpoint configured")
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(skill.endpoint, json=input_data)
            resp.raise_for_status()
            return resp.json()
    
    def get_execution(self, execution_id: str) -> Optional[SkillExecution]:
        """Get execution by ID."""
        return self._executions.get(execution_id)
    
    def get_agent_executions(self, agent_id: str) -> List[SkillExecution]:
        """Get all executions by an agent."""
        return [e for e in self._executions.values() if e.agent_id == agent_id]


class SkillRouter:
    """
    Routes tasks to appropriate skills based on task description,
    agent role, and context.
    
    Features:
    - Automatic skill discovery from ctx-vault
    - Semantic matching of tasks to skills
    - Skill chaining for complex workflows
    - Load balancing and concurrency control
    - Fallback strategies
    """
    
    def __init__(
        self,
        registry: SkillRegistry,
        context_manager: Optional["ContextManager"] = None,
    ):
        self.registry = registry
        self.context_manager = context_manager
    
    async def route_task(
        self,
        task: str,
        agent_role: AgentRole,
        context: Optional[AgentContext] = None,
        preferred_skills: Optional[List[str]] = None,
    ) -> List[Tuple[Skill, float]]:
        """
        Route a task to the best matching skills.
        
        Returns list of (skill, score) tuples sorted by relevance.
        """
        # If preferred skills specified, use those
        if preferred_skills:
            skills = []
            for sid in preferred_skills:
                skill = self.registry.get_skill(sid)
                if skill:
                    skills.append((skill, 1.0))
            return skills
        
        # Get role-appropriate skill types
        role_skill_types = self._get_role_skill_types(agent_role)
        
        # Find matching skills
        all_matches = []
        for skill_type in role_skill_types:
            matches = self.registry.find_skills(task, skill_type=skill_type)
            all_matches.extend(matches)
        
        # Deduplicate and sort
        seen = set()
        unique_matches = []
        for skill, score in all_matches:
            if skill.id not in seen:
                seen.add(skill.id)
                unique_matches.append((skill, score))
        
        unique_matches.sort(key=lambda x: x[1], reverse=True)
        
        # Boost score for skills that match context
        if context:
            unique_matches = self._boost_by_context(unique_matches, context)
        
        return unique_matches
    
    def _get_role_skill_types(self, role: AgentRole) -> List[SkillType]:
        """Get skill types appropriate for an agent role."""
        role_skills = {
            AgentRole.RESEARCHER: [SkillType.INGESTION, SkillType.SEARCH, SkillType.EXTERNAL],
            AgentRole.ANALYST: [SkillType.ANALYSIS, SkillType.SEARCH, SkillType.TRANSFORMATION],
            AgentRole.CODER: [SkillType.CODE, SkillType.ANALYSIS, SkillType.VERIFICATION],
            AgentRole.SYNTHESIZER: [SkillType.SYNTHESIS, SkillType.TRANSFORMATION, SkillType.ANALYSIS],
            AgentRole.VERIFIER: [SkillType.VERIFICATION, SkillType.ANALYSIS, SkillType.CODE],
            AgentRole.INGESTOR: [SkillType.INGESTION, SkillType.TRANSFORMATION, SkillType.EXTERNAL],
        }
        return role_skills.get(role, list(SkillType))
    
    def _boost_by_context(
        self,
        matches: List[Tuple[Skill, float]],
        context: AgentContext,
    ) -> List[Tuple[Skill, float]]:
        """Boost skill scores based on agent context."""
        boosted = []
        for skill, score in matches:
            boost = 0.0
            
            # Boost if skill tags match context metadata tags
            context_tags = context.metadata.get("tags", [])
            for tag in context_tags:
                if tag in skill.tags:
                    boost += 0.1
            
            # Boost if skill was used successfully before by this agent
            # (would check execution history)
            
            boosted.append((skill, min(score + boost, 1.0)))
        
        boosted.sort(key=lambda x: x[1], reverse=True)
        return boosted
    
    async def chain_skills(
        self,
        task: str,
        agent_role: AgentRole,
        context: Optional[AgentContext] = None,
        max_chain_length: int = 5,
    ) -> List[Skill]:
        """
        Create a chain of skills for a complex task.
        
        Returns ordered list of skills to execute sequentially.
        """
        # Get initial matches
        matches = await self.route_task(task, agent_role, context)
        
        if not matches:
            return []
        
        chain = [matches[0][0]]  # Start with best match
        
        # Build chain by finding skills that consume previous output
        current_output_schema = matches[0][0].output_schema
        
        for _ in range(max_chain_length - 1):
            # Find skills that can consume current output
            next_matches = self._find_next_skills(current_output_schema, agent_role)
            
            if not next_matches:
                break
            
            # Pick best next skill
            next_skill = next_matches[0][0]
            chain.append(next_skill)
            current_output_schema = next_skill.output_schema
            
            # Stop if we reach a terminal skill type
            if next_skill.type in (SkillType.SYNTHESIS, SkillType.VERIFICATION):
                break
        
        return chain
    
    def _find_next_skills(
        self,
        output_schema: Dict,
        role: AgentRole,
    ) -> List[Tuple[Skill, float]]:
        """Find skills that can consume the given output schema."""
        # Simplified: match by checking if output type matches input type
        # In practice, you'd do proper schema matching
        matches = []
        
        for skill in self.registry._skills.values():
            if skill.status != SkillStatus.AVAILABLE:
                continue
            
            # Check if this role can use this skill
            role_skills = self._get_role_skill_types(role)
            if skill.type not in role_skills:
                continue
            
            # Simple compatibility check
            compat_score = self._check_schema_compatibility(output_schema, skill.input_schema)
            if compat_score > 0.3:
                matches.append((skill, compat_score))
        
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches
    
    def _check_schema_compatibility(self, output_schema: Dict, input_schema: Dict) -> float:
        """Check if output schema is compatible with input schema."""
        # Simplified: check type compatibility
        out_type = output_schema.get("type", "object")
        in_type = input_schema.get("type", "object")
        
        if out_type == in_type:
            return 1.0
        
        # Object can be converted to many things
        if out_type == "object" and in_type in ("string", "array"):
            return 0.7
        
        return 0.1
    
    async def execute_skill_chain(
        self,
        chain: List[Skill],
        initial_input: Dict[str, Any],
        agent_id: Optional[str] = None,
        context: Optional[AgentContext] = None,
    ) -> List[SkillExecution]:
        """Execute a chain of skills sequentially."""
        executions = []
        current_input = initial_input
        
        for skill in chain:
            execution = await self.registry.execute_skill(skill.id, current_input, agent_id)
            executions.append(execution)
            
            if execution.status == SkillStatus.ERROR:
                # Try fallback
                fallback = await self._find_fallback(skill, current_input, agent_id)
                if fallback:
                    fallback_exec = await self.registry.execute_skill(fallback.id, current_input, agent_id)
                    executions.append(fallback_exec)
                    if fallback_exec.status != SkillStatus.ERROR:
                        current_input = fallback_exec.output_data or {}
                        continue
                break
            
            # Pass output to next skill
            if execution.output_data:
                current_input = execution.output_data
        
        return executions
    
    async def _find_fallback(
        self,
        failed_skill: Skill,
        input_data: Dict,
        agent_id: Optional[str],
    ) -> Optional[Skill]:
        """Find a fallback skill for a failed skill."""
        # Find alternative skills of same type
        alternatives = self.registry.get_skills_by_type(failed_skill.type)
        
        for alt in alternatives:
            if alt.id != failed_skill.id and alt.status == SkillStatus.AVAILABLE:
                # Quick test with small input
                test_exec = await self.registry.execute_skill(alt.id, input_data, agent_id)
                if test_exec.status != SkillStatus.ERROR:
                    return alt
        
        return None


# Default local skills
def register_default_skills(registry: SkillRegistry) -> None:
    """Register default local skills."""
    
    # Web search skill
    registry.register_local_skill(Skill(
        id="web_search",
        name="Web Search",
        type=SkillType.EXTERNAL,
        description="Search the web for information",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]},
        output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
        estimated_tokens=2000,
        tags=["web", "search", "research"],
    ))
    
    # Code generation skill
    registry.register_local_skill(Skill(
        id="code_generation",
        name="Code Generation",
        type=SkillType.CODE,
        description="Generate code from specifications",
        input_schema={"type": "object", "properties": {"spec": {"type": "string"}, "language": {"type": "string"}}, "required": ["spec"]},
        output_schema={"type": "object", "properties": {"code": {"type": "string"}, "tests": {"type": "array"}}},
        estimated_tokens=3000,
        tags=["code", "generate", "programming"],
    ))
    
    # Summarization skill
    registry.register_local_skill(Skill(
        id="summarize",
        name="Summarize",
        type=SkillType.SYNTHESIS,
        description="Summarize long text into key points",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}, "max_length": {"type": "integer"}}, "required": ["text"]},
        output_schema={"type": "object", "properties": {"summary": {"type": "string"}, "key_points": {"type": "array"}}},
        estimated_tokens=1500,
        tags=["summarize", "synthesize", "condense"],
    ))
    
    # Data extraction skill
    registry.register_local_skill(Skill(
        id="extract_data",
        name="Data Extraction",
        type=SkillType.TRANSFORMATION,
        description="Extract structured data from unstructured text",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}, "schema": {"type": "object"}}, "required": ["text"]},
        output_schema={"type": "object", "properties": {"data": {"type": "object"}}},
        estimated_tokens=2000,
        tags=["extract", "parse", "structure"],
    ))
    
    # Fact checking skill
    registry.register_local_skill(Skill(
        id="fact_check",
        name="Fact Check",
        type=SkillType.VERIFICATION,
        description="Verify claims against known sources",
        input_schema={"type": "object", "properties": {"claims": {"type": "array", "items": {"type": "string"}}}, "required": ["claims"]},
        output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
        estimated_tokens=2000,
        tags=["verify", "fact-check", "validate"],
    ))


# Convenience functions
async def create_skill_router(
    ctx_vault_url: str = "http://localhost:8000",
    api_key: Optional[str] = None,
) -> SkillRouter:
    """Create a skill router with default skills registered."""
    registry = SkillRegistry(ctx_vault_url, api_key)
    register_default_skills(registry)
    await registry.sync_from_vault()
    return SkillRouter(registry)


async def route_and_execute(
    task: str,
    agent_role: AgentRole,
    ctx_vault_url: str = "http://localhost:8000",
    api_key: Optional[str] = None,
) -> AgentResult:
    """Convenience: route task to skills and execute best match."""
    router = await create_skill_router(ctx_vault_url, api_key)
    matches = await router.route_task(task, agent_role)
    
    if not matches:
        return AgentResult(
            agent_id=f"router_{uuid.uuid4().hex[:8]}",
            role=agent_role,
            task=task,
            output=None,
            status=AgentStatus.FAILED,
            error="No matching skills found",
        )
    
    best_skill = matches[0][0]
    execution = await router.registry.execute_skill(best_skill.id, {"task": task})
    
    return AgentResult(
        agent_id=execution.id,
        role=agent_role,
        task=task,
        output=execution.output_data,
        status=AgentStatus.COMPLETED if execution.status == SkillStatus.AVAILABLE else AgentStatus.FAILED,
        error=execution.error,
        tokens_used=execution.tokens_used,
    )