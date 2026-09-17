# models.py

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index, Boolean
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)


class Base(DeclarativeBase):
    pass


class PaperEntry(Base):
    """
    <entry>
        <id>http://arxiv.org/abs/2609.11923v1</id>
        <title>GPU-CFR: 80x Faster Counterfactual Regret Minimization by Compiling the Game to Static Dataflow and CUDA Graph Replay</title>
        <updated>2026-09-10T17:58:14Z</updated>
        <link href="https://arxiv.org/abs/2609.11923v1" rel="alternate" type="text/html"/>
        <link href="https://arxiv.org/pdf/2609.11923v1" rel="related" type="application/pdf" title="pdf"/>
        <summary>Counterfactual regret minimization (CFR) is one of the few large numerical workloads that still runs faster on CPUs than on GPUs. Each iteration sweeps a game tree with up to billions of states in millions of small, interdependent gather and scatter steps issued through a generic tree interface. On a GPU every kernel finishes in microseconds, so kernel launches and framework dispatch dominate the run time, and prior GPU implementations have lost to optimized CPU code. We observe that for a fixed game, everything about a CFR iteration except the numerical values is known before the first iteration runs. We propose GPU-CFR, a compiler and runtime built on this observation. It compiles any game once into static dataflow: flat edge and information-set arrays, precomputed indices, and depth-level batched passes fix the entire operation sequence, and only solver state changes between iterations. Static chance folding, depth-level execution blocks, and a dual-lane reach buffer cut the number of framework operations by up to 18.1x. Because shapes, indices, and buffer addresses never change, CUDA Graph Replay records the iteration once and replays it with a single graph launch. On one A100, across an eight-game suite that spans card games, dice games, and board games, GPU-CFR runs 29.8--80.4x faster than the fastest prior GPU CFR on the same accelerator, and 14--258x faster than LiteEFG, one of the fastest open-source CPU implementations, on the four largest games. The compiled representation carries most of that margin: on eight CPU threads with no accelerator it is already 2.2--51.1x faster than the GPU baseline. On the CPU the optimized path reproduces the reference iterates bitwise, and tree construction and graph capture pay for themselves within the first solve. GPU-CFR beats every CPU and GPU baseline on the mid-to-large games of the suite without changing the update rule.</summary>
        <category term="cs.DC" scheme="http://arxiv.org/schemas/atom"/>
        <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
        <category term="cs.GT" scheme="http://arxiv.org/schemas/atom"/>
        <category term="cs.MS" scheme="http://arxiv.org/schemas/atom"/>
        <category term="cs.PL" scheme="http://arxiv.org/schemas/atom"/>
        <published>2026-09-10T17:58:14Z</published>
        <arxiv:primary_category term="cs.DC"/>
        <author>
            <name>Boning Li</name>
        </author>
        <author>
            <name>Longbo Huang</name>
        </author>
    </entry>
    """
    __tablename__ = "paper_entry"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    paper_quality: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    is_completed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=True,
    )

    # arxiv的id字段
    arxiv_id: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # 论文去重编码：用数据中的id字段生成的sha256
    paper_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    primary_category: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    abs_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    pdf_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.now,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "paper_sha256",
            name="uq_paper_source_external_id",
        ),
        Index(
            "idx_papers_published_at",
            "published_at",
        ),
        Index(
            "idx_papers_updated_at",
            "updated_at",
        ),
    )