"""
Agent Base Classes for ctx-vault Orchestration Framework
Async, token-aware, skill-integrated agents.
"""
import asyncio
import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Callable, Awaitable
from contextlib import asynccontextmanager

import httpx


class AgentRole(Enum):
    """Predefined agent roles for common tasks."""
    ROOT = "root"
    RESEARCHER = "researcher"
    ANALYST = "analyst"
    CODER = "coder"
    SYNTHESIZER = "synthesizer"
    VERIFIER = "verifier"
    INGESTOR = "ingestor"


class AgentStatus(Enum):
    """Agent execution status."""
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass
class TokenBudget:
    """Token budget management for agent context."""
    total: int
    used: int = 0
    reserved: int = 0
    
    @property
    def available(self) -> int:
        return self.total - self.used - self.reserved
    
    def reserve(self, tokens: int) -> bool:
        if self.available >= tokens:
            self.reserved += tokens
            return True
        return False
    
    def commit(self, tokens: int) -> None:
        self.used += tokens
        self.reserved = max(0, self.reserved - tokens)
    
    def release(self, tokens: int) -> None:
        self.reserved = max(0, self.reserved - tokens)


@dataclass
class AgentContext:
    """Execution context for an agent."""
    agent_id: str
    role: AgentRole
    task: str
    parent_id: Optional[str] = None
    token_budget: Optional[TokenBudget] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    skills: List[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    status: AgentStatus = AgentStatus.IDLE
    
    def __post_init__(self):
        if self.token_budget is None:
            self.token_budget = TokenBudget(total=8000)  # Default budget


@dataclass
class AgentResult:
    """Result from agent execution."""
    agent_id: str
    role: AgentRole
    task: str
    output: Any
    status: AgentStatus
    tokens_used: int = 0
    duration_ms: int = 0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    child_results: List["AgentResult"] = field(default_factory=list)


class SkillClient:
    """Client for interacting with ctx-vault skills API."""
    
    def __init__(self, base_url: str = "http://localhost:8000", api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
    
    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._client:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            )
        url = f"{self.base_url}{path}"
        return await self._client.request(method, url, **kwargs)
    
    async def search(self, query: str, limit: int = 10, rerank: bool = True) -> List[Dict]:
        """Search ctx-vault for relevant chunks."""
        resp = await self._request("GET", "/search", params={"q": query, "limit": limit, "rerank": rerank})
        resp.raise_for_status()
        return resp.json()
    
    async def ingest(self, source: str, source_type: str = "auto", depth: int = 1) -> Dict:
        """Ingest a new source into ctx-vault."""
        resp = await self._request("POST", "/ingest", json={
            "source": source,
            "source_type": source_type,
            "depth": depth
        })
        resp.raise_for_status()
        return resp.json()
    
    async def get_graph(self, note: str) -> Dict:
        """Get knowledge graph for a note."""
        resp = await self._request("GET", "/graph", params={"note": note})
        resp.raise_for_status()
        return resp.json()
    
    async def list_skills(self, skill_type: Optional[str] = None) -> List[Dict]:
        """List available skills."""
        params = {"type": skill_type} if skill_type else {}
        resp = await self._request("GET", "/skills", params=params)
        resp.raise_for_status()
        return resp.json()
    
    async def create_skill(self, skill_data: Dict) -> Dict:
        """Create a new skill."""
        resp = await self._request("POST", "/skills", json=skill_data)
        resp.raise_for_status()
        return resp.json()
    
    async def share_insight(self, insight_data: Dict) -> Dict:
        """Share an insight to the skill registry."""
        resp = await self._request("POST", "/insights", json=insight_data)
        resp.raise_for_status()
        return resp.json()


class BaseAgent(ABC):
    """
    Base class for all agents in the orchestration framework.
    
    Features:
    - Async execution with token budget management
    - Skill integration via ctx-vault API
    - Structured logging and error handling
    - Retry logic with exponential backoff
    - Child agent spawning
    """
    
    def __init__(
        self,
        role: AgentRole,
        ctx_vault_url: str = "http://localhost:8000",
        api_key: Optional[str] = None,
        token_budget: int = 8000,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        self.role = role
        self.ctx_vault_url = ctx_vault_url
        self.api_key = api_key
        self.default_token_budget = token_budget
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.agent_id = f"{role.value}_{uuid.uuid4().hex[:8]}"
        self._skill_client: Optional[SkillClient] = None
    
    @property
    def skill_client(self) -> SkillClient:
        if self._skill_client is None:
            self._skill_client = SkillClient(self.ctx_vault_url, self.api_key)
        return self._skill_client
    
    @abstractmethod
    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute the agent's primary task. Must be implemented by subclasses."""
        pass
    
    async def run(self, task: str, parent_context: Optional[AgentContext] = None, **kwargs) -> AgentResult:
        """Run the agent with a task and optional parent context."""
        context = AgentContext(
            agent_id=self.agent_id,
            role=self.role,
            task=task,
            parent_id=parent_context.agent_id if parent_context else None,
            token_budget=TokenBudget(total=self.default_token_budget),
            metadata=kwargs,
        )
        
        context.status = AgentStatus.RUNNING
        start_time = time.time()
        
        try:
            async with self.skill_client:
                result = await self._execute_with_retry(context)
            
            duration_ms = int((time.time() - start_time) * 1000)
            result.duration_ms = duration_ms
            return result
            
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return AgentResult(
                agent_id=self.agent_id,
                role=self.role,
                task=task,
                output=None,
                status=AgentStatus.FAILED,
                duration_ms=duration_ms,
                error=str(e),
            )
    
    async def _execute_with_retry(self, context: AgentContext) -> AgentResult:
        """Execute with retry logic."""
        last_error = None
        
        for attempt in range(self.max_retries + 1):
            try:
                context.status = AgentStatus.RUNNING if attempt == 0 else AgentStatus.RETRYING
                result = await self.execute(context)
                
                if result.status == AgentStatus.COMPLETED:
                    return result
                
                last_error = result.error or "Unknown error"
                
            except Exception as e:
                last_error = str(e)
            
            if attempt < self.max_retries:
                await asyncio.sleep(self.retry_delay * (2 ** attempt))  # Exponential backoff
        
        return AgentResult(
            agent_id=self.agent_id,
            role=self.role,
            task=context.task,
            output=None,
            status=AgentStatus.FAILED,
            error=f"Failed after {self.max_retries + 1} attempts: {last_error}",
        )
    
    async def spawn_child(
        self,
        role: AgentRole,
        task: str,
        context: AgentContext,
        **kwargs
    ) -> AgentResult:
        """Spawn a child agent to handle a subtask."""
        from agents import create_agent  # Import here to avoid circular dependency
        
        child = create_agent(role, ctx_vault_url=self.ctx_vault_url, api_key=self.api_key)
        
        # Reserve tokens for child
        child_budget = 0
        if context.token_budget:
            child_budget = min(2000, context.token_budget.available // 2)
            if not context.token_budget.reserve(child_budget):
                return AgentResult(
                    agent_id=f"{role.value}_failed",
                    role=role,
                    task=task,
                    output=None,
                    status=AgentStatus.FAILED,
                    error="Insufficient token budget for child agent",
                )
        
        child_context = AgentContext(
            agent_id=f"{role.value}_{uuid.uuid4().hex[:8]}",
            role=role,
            task=task,
            parent_id=context.agent_id,
            token_budget=TokenBudget(total=child_budget) if context.token_budget else None,
            metadata={**context.metadata, **kwargs},
        )
        
        result = await child.run(task, parent_context=child_context)
        
        # Commit tokens used by child
        if context.token_budget and result.tokens_used:
            context.token_budget.commit(result.tokens_used)
        
        return result
    
    async def search_vault(self, query: str, limit: int = 10, context: Optional[AgentContext] = None) -> List[Dict]:
        """Search ctx-vault with token budget awareness."""
        estimated_tokens = limit * 200
        if context and context.token_budget:
            # Estimate tokens for search results
            if not context.token_budget.reserve(estimated_tokens):
                return []
        
        results = await self.skill_client.search(query, limit)
        
        if context and context.token_budget:
            actual_tokens = len(str(results)) // 4  # Rough estimate
            context.token_budget.commit(min(actual_tokens, estimated_tokens))
            context.token_budget.release(max(0, estimated_tokens - actual_tokens))
        
        return results
    
    async def ingest_source(self, source: str, source_type: str = "auto", context: Optional[AgentContext] = None) -> Dict:
        """Ingest a source into ctx-vault."""
        return await self.skill_client.ingest(source, source_type)


class ResearcherAgent(BaseAgent):
    """Agent specialized in information gathering and research."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(AgentRole.RESEARCHER, *args, **kwargs)
    
    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute research task: search, gather, and synthesize information."""
        query = context.task
        
        # Search vault for existing knowledge
        vault_results = await self.search_vault(query, limit=10, context=context)
        
        # If insufficient results, try to ingest new sources
        if len(vault_results) < 3:
            # Try to find and ingest relevant web sources
            # This would use web search + ingestion in practice
            pass
        
        # Synthesize findings
        findings = {
            "query": query,
            "vault_results_count": len(vault_results),
            "results": vault_results[:5],  # Top 5
            "summary": self._summarize_results(vault_results),
        }
        
        return AgentResult(
            agent_id=self.agent_id,
            role=self.role,
            task=query,
            output=findings,
            status=AgentStatus.COMPLETED,
            tokens_used=len(str(findings)) // 4,
        )
    
    def _summarize_results(self, results: List[Dict]) -> str:
        if not results:
            return "No relevant information found in vault."
        
        summaries = []
        for r in results[:3]:
            title = r.get("title", "Untitled")
            content = r.get("text", "")[:200]
            summaries.append(f"- {title}: {content}...")
        
        return "\n".join(summaries)


class AnalystAgent(BaseAgent):
    """Agent specialized in analysis, comparison, and synthesis."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(AgentRole.ANALYST, *args, **kwargs)
    
    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute analysis task: compare, contrast, and derive insights."""
        # Get research data from context
        research_data = context.metadata.get("research_data", {})
        vault_results = research_data.get("results", [])
        
        # Perform analysis based on task
        analysis = {
            "task": context.task,
            "sources_analyzed": len(vault_results),
            "key_findings": self._extract_key_findings(vault_results),
            "comparisons": self._perform_comparisons(vault_results),
            "gaps": self._identify_gaps(vault_results),
        }
        
        return AgentResult(
            agent_id=self.agent_id,
            role=self.role,
            task=context.task,
            output=analysis,
            status=AgentStatus.COMPLETED,
            tokens_used=len(str(analysis)) // 4,
        )
    
    def _extract_key_findings(self, results: List[Dict]) -> List[str]:
        findings = []
        for r in results[:5]:
            text = r.get("text", "")
            # Extract first sentence as finding
            sentences = text.split(". ")
            if sentences:
                findings.append(sentences[0] + ".")
        return findings[:5]
    
    def _perform_comparisons(self, results: List[Dict]) -> List[Dict]:
        # Placeholder for comparison logic
        return []
    
    def _identify_gaps(self, results: List[Dict]) -> List[str]:
        # Placeholder for gap detection
        return ["Further research needed on implementation details"]


class CoderAgent(BaseAgent):
    """Agent specialized in code generation and verification."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(AgentRole.CODER, *args, **kwargs)
    
    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute coding task: generate, verify, and test code."""
        spec = context.task
        
        # Search for relevant patterns in vault
        patterns = await self.search_vault(f"code pattern {spec}", limit=5, context=context)
        
        # Generate code (placeholder - would use LLM in practice)
        code = self._generate_code(spec, patterns)
        
        # Verify code (placeholder)
        verification = self._verify_code(code)
        
        output = {
            "specification": spec,
            "code": code,
            "verification": verification,
            "patterns_used": len(patterns),
        }
        
        return AgentResult(
            agent_id=self.agent_id,
            role=self.role,
            task=spec,
            output=output,
            status=AgentStatus.COMPLETED if verification.get("valid") else AgentStatus.FAILED,
            tokens_used=len(str(output)) // 4,
        )
    
    def _generate_code(self, spec: str, patterns: List[Dict]) -> str:
        # Placeholder - would use LLM with patterns as context
        return f"# Generated code for: {spec}\n# Patterns found: {len(patterns)}\npass"
    
    def _verify_code(self, code: str) -> Dict:
        # Placeholder - would run syntax check, tests, etc.
        return {"valid": True, "issues": []}


class SynthesizerAgent(BaseAgent):
    """Agent specialized in integrating multiple sources into coherent output."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(AgentRole.SYNTHESIZER, *args, **kwargs)
    
    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute synthesis task: combine child results into final output."""
        child_results = context.metadata.get("child_results", [])
        
        # Synthesize all child outputs
        synthesis = {
            "task": context.task,
            "sources_integrated": len(child_results),
            "combined_output": self._integrate_outputs(child_results),
            "confidence": self._calculate_confidence(child_results),
        }
        
        return AgentResult(
            agent_id=self.agent_id,
            role=self.role,
            task=context.task,
            output=synthesis,
            status=AgentStatus.COMPLETED,
            tokens_used=len(str(synthesis)) // 4,
        )
    
    def _integrate_outputs(self, results: List[AgentResult]) -> str:
        sections = []
        for r in results:
            if r.output:
                sections.append(f"## {r.role.value.upper()}\n{json.dumps(r.output, indent=2)}")
        return "\n\n".join(sections)
    
    def _calculate_confidence(self, results: List[AgentResult]) -> float:
        if not results:
            return 0.0
        completed = sum(1 for r in results if r.status == AgentStatus.COMPLETED)
        return completed / len(results)


# Agent factory
def create_agent(role: AgentRole, **kwargs) -> BaseAgent:
    """Factory function to create agents by role."""
    agents = {
        AgentRole.RESEARCHER: ResearcherAgent,
        AgentRole.ANALYST: AnalystAgent,
        AgentRole.CODER: CoderAgent,
        AgentRole.SYNTHESIZER: SynthesizerAgent,
        AgentRole.VERIFIER: AnalystAgent,  # Reuse analyst for verification
        AgentRole.INGESTOR: ResearcherAgent,  # Reuse researcher for ingestion
    }
    
    agent_class = agents.get(role, BaseAgent)
    return agent_class(**kwargs)