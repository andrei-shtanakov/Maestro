"""Tests for inter-agent messaging functionality.

This module contains unit tests for message CRUD operations, MCP server
message tools, REST API endpoints, and integration tests for broadcast
messaging.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.coordination.mcp_server import (
    MCPServer,
    create_mcp_server,
)
from maestro.coordination.rest_api import RESTServer, create_rest_server
from maestro.database import (
    Database,
    MessageNotFoundError,
    create_database,
)
from maestro.models import Message


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def db(temp_db_path: Path) -> AsyncGenerator[Database, None]:
    """Provide a connected and initialized database."""
    database = await create_database(temp_db_path)
    yield database
    await database.close()


@pytest.fixture
async def mcp_server(db: Database) -> MCPServer:
    """Provide an MCP server instance."""
    return create_mcp_server(db)


@pytest.fixture
async def rest_server(db: Database) -> RESTServer:
    """Provide a REST server instance."""
    return create_rest_server(db)


@pytest.fixture
async def client(rest_server: RESTServer) -> AsyncGenerator[AsyncClient, None]:
    """Provide an async HTTP client for testing REST endpoints."""
    transport = ASGITransport(app=rest_server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def sample_message() -> Message:
    """Provide a sample message for testing."""
    return Message(
        from_agent="agent-001",
        to_agent="agent-002",
        message="Hello from agent-001!",
    )


@pytest.fixture
def broadcast_message() -> Message:
    """Provide a sample broadcast message for testing."""
    return Message(
        from_agent="agent-001",
        to_agent=None,  # Broadcast
        message="This is a broadcast to all agents!",
    )


@pytest.fixture
async def saved_message(db: Database, sample_message: Message) -> Message:
    """Create and return a saved message in the database."""
    return await db.save_message(sample_message)


@pytest.fixture
async def saved_broadcast(db: Database, broadcast_message: Message) -> Message:
    """Create and return a saved broadcast message in the database."""
    return await db.save_message(broadcast_message)


# =============================================================================
# Unit Tests: Database Message CRUD
# =============================================================================


class TestDatabaseMessageCRUD:
    """Tests for database message CRUD operations."""

    @pytest.mark.anyio
    async def test_save_message_returns_message_with_id(
        self, db: Database, sample_message: Message
    ) -> None:
        """Test that saving a message returns it with a generated ID."""
        saved = await db.save_message(sample_message)

        assert saved.id is not None
        assert saved.id > 0
        assert saved.from_agent == sample_message.from_agent
        assert saved.to_agent == sample_message.to_agent
        assert saved.message == sample_message.message
        assert saved.read is False

    @pytest.mark.anyio
    async def test_save_broadcast_message(
        self, db: Database, broadcast_message: Message
    ) -> None:
        """Test saving a broadcast message (to_agent=None)."""
        saved = await db.save_message(broadcast_message)

        assert saved.id is not None
        assert saved.to_agent is None
        assert saved.message == broadcast_message.message

    @pytest.mark.anyio
    async def test_get_message_by_id(
        self, db: Database, saved_message: Message
    ) -> None:
        """Test retrieving a message by ID."""
        assert saved_message.id is not None
        retrieved = await db.get_message(saved_message.id)

        assert retrieved.id == saved_message.id
        assert retrieved.from_agent == saved_message.from_agent
        assert retrieved.to_agent == saved_message.to_agent
        assert retrieved.message == saved_message.message

    @pytest.mark.anyio
    async def test_get_nonexistent_message_raises_error(self, db: Database) -> None:
        """Test that getting a non-existent message raises MessageNotFoundError."""
        with pytest.raises(MessageNotFoundError):
            await db.get_message(99999)

    @pytest.mark.anyio
    async def test_get_messages_for_agent_returns_direct_messages(
        self, db: Database
    ) -> None:
        """Test that get_messages_for_agent returns messages to that agent."""
        # Create messages to different agents
        await db.save_message(
            Message(
                from_agent="agent-001", to_agent="agent-002", message="To agent-002"
            )
        )
        await db.save_message(
            Message(
                from_agent="agent-001", to_agent="agent-003", message="To agent-003"
            )
        )

        messages = await db.get_messages_for_agent("agent-002")

        assert len(messages) == 1
        assert messages[0].to_agent == "agent-002"

    @pytest.mark.anyio
    async def test_get_messages_for_agent_includes_broadcasts(
        self, db: Database, saved_broadcast: Message
    ) -> None:
        """Test that get_messages_for_agent includes broadcast messages."""
        # Create a direct message to a different agent
        await db.save_message(
            Message(
                from_agent="agent-001", to_agent="agent-003", message="To agent-003"
            )
        )

        messages = await db.get_messages_for_agent("agent-002")

        # Should include the broadcast but not the message to agent-003
        assert len(messages) == 1
        assert messages[0].to_agent is None

    @pytest.mark.anyio
    async def test_get_messages_for_agent_unread_only(self, db: Database) -> None:
        """Test filtering for unread messages only."""
        # Create messages
        msg1 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Message 1")
        )
        await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Message 2")
        )

        # Mark one as read
        assert msg1.id is not None
        await db.mark_message_read(msg1.id)

        # Get unread only
        messages = await db.get_messages_for_agent("agent-002", unread_only=True)

        assert len(messages) == 1
        assert messages[0].message == "Message 2"

    @pytest.mark.anyio
    async def test_get_all_messages(self, db: Database) -> None:
        """Test retrieving all messages."""
        await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 1")
        )
        await db.save_message(
            Message(from_agent="agent-002", to_agent="agent-001", message="Msg 2")
        )
        await db.save_message(
            Message(from_agent="agent-003", to_agent=None, message="Broadcast")
        )

        messages = await db.get_all_messages()

        assert len(messages) == 3

    @pytest.mark.anyio
    async def test_mark_message_read(
        self, db: Database, saved_message: Message
    ) -> None:
        """Test marking a message as read."""
        assert saved_message.id is not None
        assert saved_message.read is False

        updated = await db.mark_message_read(saved_message.id)

        assert updated.read is True

    @pytest.mark.anyio
    async def test_mark_nonexistent_message_read_raises_error(
        self, db: Database
    ) -> None:
        """Test that marking non-existent message raises error."""
        with pytest.raises(MessageNotFoundError):
            await db.mark_message_read(99999)

    @pytest.mark.anyio
    async def test_mark_messages_read_multiple(self, db: Database) -> None:
        """Test marking multiple messages as read."""
        msg1 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 1")
        )
        msg2 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 2")
        )
        msg3 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 3")
        )

        assert msg1.id is not None
        assert msg2.id is not None
        assert msg3.id is not None

        count = await db.mark_messages_read([msg1.id, msg2.id])

        assert count == 2

        # Verify
        retrieved1 = await db.get_message(msg1.id)
        retrieved2 = await db.get_message(msg2.id)
        retrieved3 = await db.get_message(msg3.id)

        assert retrieved1.read is True
        assert retrieved2.read is True
        assert retrieved3.read is False

    @pytest.mark.anyio
    async def test_mark_messages_read_empty_list(self, db: Database) -> None:
        """Test marking empty list returns zero count."""
        count = await db.mark_messages_read([])
        assert count == 0

    @pytest.mark.anyio
    async def test_delete_message(self, db: Database, saved_message: Message) -> None:
        """Test deleting a message."""
        assert saved_message.id is not None
        result = await db.delete_message(saved_message.id)

        assert result is True

        with pytest.raises(MessageNotFoundError):
            await db.get_message(saved_message.id)

    @pytest.mark.anyio
    async def test_delete_nonexistent_message(self, db: Database) -> None:
        """Test deleting non-existent message returns False."""
        result = await db.delete_message(99999)
        assert result is False


# =============================================================================
# Unit Tests: MCP Server Message Tools
# =============================================================================


class TestMCPPostMessage:
    """Tests for post_message MCP tool."""

    @pytest.mark.anyio
    async def test_post_message_succeeds(self, mcp_server: MCPServer) -> None:
        """Test posting a message successfully."""
        result = await mcp_server.post_message(
            agent_id="agent-001",
            message="Hello agent-002!",
            to_agent="agent-002",
        )

        assert result.success is True
        assert result.message is not None
        assert result.message.from_agent == "agent-001"
        assert result.message.to_agent == "agent-002"
        assert result.message.message == "Hello agent-002!"
        assert result.error is None

    @pytest.mark.anyio
    async def test_post_broadcast_message(self, mcp_server: MCPServer) -> None:
        """Test posting a broadcast message (to_agent=None)."""
        result = await mcp_server.post_message(
            agent_id="agent-001",
            message="Broadcast to all!",
            to_agent=None,
        )

        assert result.success is True
        assert result.message is not None
        assert result.message.to_agent is None


class TestMCPReadMessages:
    """Tests for read_messages MCP tool."""

    @pytest.mark.anyio
    async def test_read_messages_returns_messages_for_agent(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test reading messages returns messages for the agent."""
        # Post some messages
        await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 1")
        )
        await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 2")
        )
        await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-003", message="Msg 3")
        )

        result = await mcp_server.read_messages("agent-002", unread_only=False)

        assert result.success is True
        assert result.count == 2
        assert len(result.messages) == 2

    @pytest.mark.anyio
    async def test_read_messages_includes_broadcasts(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test reading messages includes broadcast messages."""
        await db.save_message(
            Message(from_agent="agent-001", to_agent=None, message="Broadcast!")
        )

        result = await mcp_server.read_messages("agent-002", unread_only=False)

        assert result.success is True
        assert result.count == 1
        assert result.messages[0].to_agent is None

    @pytest.mark.anyio
    async def test_read_messages_unread_only(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test filtering unread messages."""
        msg = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 1")
        )
        await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 2")
        )

        assert msg.id is not None
        await db.mark_message_read(msg.id)

        result = await mcp_server.read_messages("agent-002", unread_only=True)

        assert result.success is True
        assert result.count == 1

    @pytest.mark.anyio
    async def test_read_messages_empty(self, mcp_server: MCPServer) -> None:
        """Test reading messages when none exist."""
        result = await mcp_server.read_messages("agent-002", unread_only=False)

        assert result.success is True
        assert result.count == 0
        assert result.messages == []


class TestMCPMarkMessagesRead:
    """Tests for mark_messages_read MCP tool."""

    @pytest.mark.anyio
    async def test_mark_messages_read_succeeds(
        self, mcp_server: MCPServer, db: Database
    ) -> None:
        """Test marking messages as read."""
        msg1 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 1")
        )
        msg2 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 2")
        )

        assert msg1.id is not None
        assert msg2.id is not None

        result = await mcp_server.mark_messages_read("agent-002", [msg1.id, msg2.id])

        assert result.success is True
        assert result.count == 2

    @pytest.mark.anyio
    async def test_mark_messages_read_empty_list(self, mcp_server: MCPServer) -> None:
        """Test marking empty list returns zero count."""
        result = await mcp_server.mark_messages_read("agent-002", [])

        assert result.success is True
        assert result.count == 0


# =============================================================================
# Unit Tests: REST API Message Endpoints
# =============================================================================


class TestRESTPostMessage:
    """Tests for POST /messages endpoint."""

    @pytest.mark.anyio
    async def test_post_message_succeeds(self, client: AsyncClient) -> None:
        """Test posting a message via REST API."""
        response = await client.post(
            "/messages",
            json={
                "from_agent": "agent-001",
                "to_agent": "agent-002",
                "message": "Hello!",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["message"]["from_agent"] == "agent-001"
        assert data["message"]["to_agent"] == "agent-002"

    @pytest.mark.anyio
    async def test_post_broadcast_message(self, client: AsyncClient) -> None:
        """Test posting a broadcast message via REST API."""
        response = await client.post(
            "/messages",
            json={
                "from_agent": "agent-001",
                "to_agent": None,
                "message": "Broadcast!",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["message"]["to_agent"] is None


class TestRESTGetMessages:
    """Tests for GET /messages endpoint."""

    @pytest.mark.anyio
    async def test_get_messages(self, client: AsyncClient, db: Database) -> None:
        """Test getting messages via REST API."""
        await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 1")
        )

        response = await client.get(
            "/messages",
            params={"agent_id": "agent-002", "unread_only": "false"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["count"] == 1

    @pytest.mark.anyio
    async def test_get_messages_missing_agent_id(self, client: AsyncClient) -> None:
        """Test that missing agent_id returns 422."""
        response = await client.get("/messages")

        assert response.status_code == 422


class TestRESTGetMessage:
    """Tests for GET /messages/{message_id} endpoint."""

    @pytest.mark.anyio
    async def test_get_message_by_id(
        self, client: AsyncClient, saved_message: Message
    ) -> None:
        """Test getting a specific message by ID."""
        assert saved_message.id is not None
        response = await client.get(f"/messages/{saved_message.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == saved_message.id
        assert data["from_agent"] == saved_message.from_agent

    @pytest.mark.anyio
    async def test_get_nonexistent_message(self, client: AsyncClient) -> None:
        """Test getting non-existent message returns 404."""
        response = await client.get("/messages/99999")

        assert response.status_code == 404


class TestRESTMarkMessageRead:
    """Tests for PUT /messages/{message_id}/read endpoint."""

    @pytest.mark.anyio
    async def test_mark_message_read(
        self, client: AsyncClient, saved_message: Message
    ) -> None:
        """Test marking a message as read."""
        assert saved_message.id is not None
        response = await client.put(f"/messages/{saved_message.id}/read")

        assert response.status_code == 200
        data = response.json()
        assert data["read"] is True

    @pytest.mark.anyio
    async def test_mark_nonexistent_message_read(self, client: AsyncClient) -> None:
        """Test marking non-existent message returns 404."""
        response = await client.put("/messages/99999/read")

        assert response.status_code == 404


class TestRESTMarkMessagesRead:
    """Tests for PUT /messages/read endpoint."""

    @pytest.mark.anyio
    async def test_mark_messages_read(self, client: AsyncClient, db: Database) -> None:
        """Test marking multiple messages as read."""
        msg1 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 1")
        )
        msg2 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Msg 2")
        )

        assert msg1.id is not None
        assert msg2.id is not None

        response = await client.put(
            "/messages/read",
            json={"agent_id": "agent-002", "message_ids": [msg1.id, msg2.id]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["count"] == 2

    @pytest.mark.anyio
    async def test_mark_messages_read_unauthorized(
        self, client: AsyncClient, db: Database
    ) -> None:
        """Test that agents cannot mark messages addressed to other agents."""
        msg = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Private")
        )

        assert msg.id is not None

        # agent-003 tries to mark agent-002's message as read
        response = await client.put(
            "/messages/read",
            json={"agent_id": "agent-003", "message_ids": [msg.id]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        # Count should be 0 since agent-003 is not authorized
        assert data["count"] == 0

        # Verify message is still unread
        retrieved = await db.get_message(msg.id)
        assert retrieved.read is False


# =============================================================================
# Integration Tests: Broadcast Messaging
# =============================================================================


class TestBroadcastMessaging:
    """Integration tests for broadcast message functionality."""

    @pytest.mark.anyio
    async def test_broadcast_visible_to_all_agents(self, db: Database) -> None:
        """Test that broadcast messages are visible to all agents."""
        # Post a broadcast message
        broadcast = await db.save_message(
            Message(
                from_agent="admin",
                to_agent=None,
                message="System maintenance in 5 minutes",
            )
        )

        # Verify all agents can see it
        for agent_id in ["agent-001", "agent-002", "agent-003"]:
            messages = await db.get_messages_for_agent(agent_id)
            assert len(messages) == 1
            assert messages[0].id == broadcast.id
            assert messages[0].to_agent is None

    @pytest.mark.anyio
    async def test_broadcast_mixed_with_direct_messages(self, db: Database) -> None:
        """Test that broadcasts are returned alongside direct messages."""
        # Create broadcast
        await db.save_message(
            Message(from_agent="admin", to_agent=None, message="Broadcast")
        )

        # Create direct messages
        await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Direct 1")
        )
        await db.save_message(
            Message(from_agent="agent-003", to_agent="agent-002", message="Direct 2")
        )
        await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-003", message="To agent-3")
        )

        # Agent-002 should see broadcast + 2 direct messages
        messages_002 = await db.get_messages_for_agent("agent-002")
        assert len(messages_002) == 3

        # Agent-003 should see broadcast + 1 direct message
        messages_003 = await db.get_messages_for_agent("agent-003")
        assert len(messages_003) == 2

        # Agent-001 should only see broadcast (no messages to them)
        messages_001 = await db.get_messages_for_agent("agent-001")
        assert len(messages_001) == 1
        assert messages_001[0].to_agent is None

    @pytest.mark.anyio
    async def test_broadcast_through_mcp_server(self, mcp_server: MCPServer) -> None:
        """Test broadcast flow through MCP server tools."""
        # Post broadcast
        post_result = await mcp_server.post_message(
            agent_id="coordinator",
            message="Task assignment complete",
            to_agent=None,
        )
        assert post_result.success is True
        assert post_result.message is not None
        broadcast_id = post_result.message.id

        # Read from multiple agents
        result_001 = await mcp_server.read_messages("agent-001", unread_only=False)
        result_002 = await mcp_server.read_messages("agent-002", unread_only=False)

        assert result_001.success is True
        assert result_002.success is True
        assert len(result_001.messages) == 1
        assert len(result_002.messages) == 1
        assert result_001.messages[0].id == broadcast_id
        assert result_002.messages[0].id == broadcast_id

    @pytest.mark.anyio
    async def test_broadcast_through_rest_api(
        self, client: AsyncClient, db: Database
    ) -> None:
        """Test broadcast flow through REST API."""
        # Post broadcast
        post_response = await client.post(
            "/messages",
            json={
                "from_agent": "coordinator",
                "to_agent": None,
                "message": "All tasks complete!",
            },
        )
        assert post_response.status_code == 200
        post_data = post_response.json()
        assert post_data["success"] is True

        # Read from multiple agents
        for agent_id in ["agent-001", "agent-002"]:
            get_response = await client.get(
                "/messages",
                params={"agent_id": agent_id, "unread_only": "false"},
            )
            assert get_response.status_code == 200
            data = get_response.json()
            assert data["count"] == 1
            assert data["messages"][0]["to_agent"] is None


# =============================================================================
# Integration Tests: Message Ordering
# =============================================================================


class TestMessageOrdering:
    """Tests for message ordering (most recent first)."""

    @pytest.mark.anyio
    async def test_messages_ordered_by_created_at_desc(self, db: Database) -> None:
        """Test that messages are returned in reverse chronological order."""
        import asyncio

        # Create messages with slight delay to ensure different timestamps
        msg1 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="First")
        )
        await asyncio.sleep(0.01)
        msg2 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Second")
        )
        await asyncio.sleep(0.01)
        msg3 = await db.save_message(
            Message(from_agent="agent-001", to_agent="agent-002", message="Third")
        )

        messages = await db.get_messages_for_agent("agent-002")

        assert len(messages) == 3
        # Most recent first
        assert messages[0].id == msg3.id
        assert messages[1].id == msg2.id
        assert messages[2].id == msg1.id
