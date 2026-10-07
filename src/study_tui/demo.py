"""An explicit offline DB-index course for first-run exploration and QA.

These fixtures are authored examples, not the result of a subscribed CLI call.
"""
from __future__ import annotations

import copy

from .models import validate_lesson, validate_plan


DEMO_PLAN = {
    "schema_version": 1,
    "topic": "PostgreSQL B-tree 인덱스",
    "title": "인덱스의 비용을 이해하고 실행 계획으로 선택하기",
    "objectives": ["B-tree 탐색과 테이블 접근을 구분한다", "복합 인덱스 후보를 조건·정렬·쓰기 비용으로 비교한다"],
    "scope": "B-tree와 복합 인덱스의 기본 판단까지 2파트로 완료합니다. 전문 검색, 파티셔닝, 내부 페이지 구현은 심화 과정의 범위입니다.",
    "parts": [
        {"ordinal": 1, "title": "B-tree: 빨리 찾는 대가", "objectives": ["탐색 경로를 설명한다", "선택도와 쓰기 비용을 함께 판단한다"], "minutes": 35},
        {"ordinal": 2, "title": "복합 인덱스와 EXPLAIN으로 검증하기", "objectives": ["열 순서와 정렬의 관계를 판단한다", "추정 행 수와 실제 행 수를 비교한다"], "minutes": 40},
    ],
}

_TREE = """                 [ Root: 40 ]
                  /        \\
                 v          v
        [ 10 | 20 | 30 ]   [ 40 | 50 | 60 ]
                 |          |
                 v          v
          [ table rows ]  [ table rows ]"""

_LOOKUP = """ Query: customer_id = 42
           |
           v
 +----------------------+       +--------------------+
 | Index: key + row refs | ----> | Table: needed rows |
 +----------------------+       +--------------------+
           |
           v
 cost = index traversal + matching table access"""

_PREFIX = """ Index order: (customer_id, created_at)

 customer 7                     customer 42
 +------------------+           +------------------+
 | 2026-01 -> row A  |           | 2026-01 -> row C  |
 | 2026-02 -> row B  |           | 2026-02 -> row D  |
 +------------------+           +------------------+
                                      ^
                                      |
      customer_id = 42 selects one ordered range"""

_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 700 210" role="img">
<title>인덱스 탐색과 테이블 접근</title><desc>조건으로 인덱스를 찾고 필요한 행만 테이블에서 읽는다.</desc>
<rect x="10" y="45" width="290" height="110" fill="white" stroke="black" stroke-width="3"/>
<rect x="400" y="45" width="290" height="110" fill="white" stroke="black" stroke-width="3"/>
<path d="M300 100 H395 M380 88 L395 100 L380 112" fill="none" stroke="black" stroke-width="3"/>
<text x="155" y="88" text-anchor="middle" font-size="24">인덱스: 키 + 행 참조</text>
<text x="155" y="124" text-anchor="middle" font-size="20">customer_id = 42</text>
<text x="545" y="88" text-anchor="middle" font-size="24">테이블: 필요한 행</text>
<text x="545" y="124" text-anchor="middle" font-size="20">일치하는 행만 접근</text>
</svg>"""

_SOURCES = [
    {"id": "s1", "title": "PostgreSQL: Index Types", "url": "https://www.postgresql.org/docs/current/indexes-types.html", "checked_at": None},
    {"id": "s2", "title": "PostgreSQL: Indexes", "url": "https://www.postgresql.org/docs/current/indexes.html", "checked_at": None},
    {"id": "s3", "title": "PostgreSQL: Using EXPLAIN", "url": "https://www.postgresql.org/docs/current/using-explain.html", "checked_at": None},
]

DEMO_LESSONS = {
    1: {
        "schema_version": 1, "title": "B-tree: 빨리 찾는 대가", "minutes": 35,
        "sections": [
            {"kind": "concept", "title": "인덱스는 별도의 탐색 자료구조다 · 7분",
             "body_markdown": "인덱스는 테이블을 대신하는 정답 목록이 아니라, 조건에 맞는 행을 찾기 위한 별도의 자료구조입니다. PostgreSQL의 일반적인 B-tree 인덱스는 정렬된 키와 행 위치를 이용해 동등 조건과 범위 조건을 처리합니다.\n\n전화번호부의 이름을 보고 전화번호를 찾듯, 먼저 키 범위를 좁힌 뒤 필요한 행을 읽습니다. 테이블 대부분이 결과라면 인덱스를 따라 여러 페이지를 읽는 것보다 순차적으로 읽는 편이 유리할 수 있습니다. 인덱스가 존재한다는 이유만으로 항상 사용되지는 않습니다.\n\n읽기 전에 적어 보기: `customer_id = 42`인 주문 조회와 전체 주문 합계 조회에서 필요한 행의 비율은 어떻게 다를까요? 다음 원리 절의 그림에서 `Root`, 정렬된 키, `table rows`가 각각 어디에 대응하는지 설명해 보세요.",
             "visual_ids": [], "source_ids": ["s1", "s2"]},
            {"kind": "mechanism", "title": "경로를 좁히고, 필요한 행을 읽는다 · 9분",
             "body_markdown": "그림 v1은 비교를 통해 키 범위를 좁히는 탐색 경로를 단순화했습니다. 실제 B-tree 페이지에는 훨씬 많은 키가 있고 구현 세부는 다르지만, 모든 행을 하나씩 비교하지 않고 자식 범위를 고른다는 핵심을 보여 줍니다.\n\n그림 v2에서는 인덱스의 키/행 참조를 찾는 단계와 테이블에서 데이터를 얻는 단계를 분리합니다. 인덱스 탐색이 빠르더라도 일치하는 행이 많으면 테이블 접근 비용이 커집니다. **선택도**, 데이터 분포, 캐시와 저장 장치 특성, 통계가 계획 선택에 영향을 줍니다.\n\n삽입·삭제·인덱스 열의 변경은 인덱스 유지 작업을 동반할 수 있습니다. 인덱스는 저장 공간과 쓰기 비용을 추가합니다. ‘읽기가 빨라졌다’는 측정만으로 인덱스 추가를 결정하지 말고 쓰기가 많은 시간대도 함께 살펴보세요.\n\n3분 활동: 인덱스를 통해 5행을 읽는 경우와 50만 행을 읽는 경우를 그림 v2에 표시하고, 어떤 비용 항목이 커지는지 비교하세요.",
             "visual_ids": ["v1", "v2"], "source_ids": ["s1", "s2"]},
            {"kind": "example", "title": "주문 조회 후보를 먼저 측정한다 · 12분",
             "body_markdown": "아래는 연습용 SQL입니다. 자신의 운영 DB에 바로 적용하지 말고 별도 실습 DB 또는 종이에서 계획 후보를 비교하세요. 예시의 테이블/데이터는 사용자가 준비해야 합니다.\n\n```sql\nCREATE TABLE orders (\n  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n  customer_id bigint NOT NULL,\n  created_at timestamptz NOT NULL,\n  total_cents integer NOT NULL\n);\n\nEXPLAIN\nSELECT id, created_at, total_cents\nFROM orders\nWHERE customer_id = 42;\n\nCREATE INDEX orders_customer_idx\nON orders (customer_id);\n\nANALYZE orders;\nEXPLAIN\nSELECT id, created_at, total_cents\nFROM orders\nWHERE customer_id = 42;\n```\n\n데이터가 적으면 인덱스를 만든 뒤에도 순차 스캔이 나올 수 있습니다. 그것만으로 실패라고 판단하지 마세요. 조건에 맞는 행이 적은 고객과 많은 고객을 비교하고 추정 행 수(`rows`)가 어떻게 달라지는지 확인합니다.\n\n시간 배분: 실행 계획 두 개를 4분 동안 비교하고, 조건에 맞는 행 수가 많은 상황을 4분 동안 예상하세요. 마지막 4분은 ‘읽기 이득 / 쓰기 비용 / 공간 비용’ 세 열로 후보 인덱스의 장단점을 메모합니다. `EXPLAIN ANALYZE`는 쿼리를 실제 실행하므로 다음 파트에서 안전한 실습 데이터로만 사용합니다.",
             "visual_ids": [], "source_ids": ["s2", "s3"]},
            {"kind": "summary", "title": "판단 기준과 이해도 점검 · 7분",
             "body_markdown": "B-tree는 정렬된 키 범위를 이용해 후보 행을 좁힙니다. 전체 비용에는 인덱스 탐색과 필요한 데이터 접근이 모두 포함됩니다. 반환 행 비율과 데이터 분포를 살피고, 추가 저장 공간과 쓰기 유지 비용을 함께 비교하세요.\n\n이 파트는 키 탐색과 테이블 접근을 구분하는 데서 끝납니다. 아래 퀴즈를 종이에 풀거나 답 메모에 적고 정답을 확인하세요. 답을 적거나 공개하지 않고 다음 파트로 이동해도 됩니다. 다음 파트에서는 복합 인덱스의 열 순서를 실행 계획으로 검증합니다.",
             "visual_ids": [], "source_ids": ["s1", "s3"]},
        ],
        "visuals": [
            {"id": "v1", "kind": "mermaid", "source": "flowchart TD\n  R[루트: 비교 키] --> L[작은 키 범위]\n  R --> H[큰 키 범위]\n  L --> A[필요한 테이블 행]\n  H --> B[필요한 테이블 행]", "caption": "v1. Root는 비교 시작점, 정렬된 키 범위는 탐색 후보, table rows는 필요한 행을 뜻합니다. 구현 세부를 생략한 개념도입니다.", "alt_text": "루트의 비교 결과로 키 범위를 고르고 해당 테이블 행에 접근하는 구조", "fallbacks": [{"kind": "ascii", "source": _TREE}]},
            {"id": "v2", "kind": "svg", "source": _SVG, "caption": "v2. 조건으로 인덱스를 탐색한 다음 필요한 테이블 행에 접근합니다. 두 단계의 비용을 함께 비교합니다.", "alt_text": "customer_id 42를 인덱스에서 찾은 뒤 필요한 테이블 행에 접근", "fallbacks": [{"kind": "ascii", "source": _LOOKUP}]},
        ],
        "quizzes": [
            {"id": "q1", "question": "테이블 행의 80%를 반환하는 조회에서 B-tree 인덱스가 항상 순차 스캔보다 빠를까요? 비용을 두 가지 이상 들어 설명하세요.", "answer": "항상 빠르지 않습니다. 인덱스 탐색과 많은 테이블 행 접근을 합한 비용이 순차 읽기보다 클 수 있습니다.", "explanation": "반환 행 수, 페이지 접근 방식, 캐시/저장 장치, 통계와 데이터 분포를 고려해야 합니다. 인덱스의 존재만으로 계획 사용을 보장할 수 없습니다."},
            {"id": "q2", "question": "읽기 성능이 좋아진 인덱스를 추가할 때 함께 확인할 쓰기 측면의 비용은 무엇인가요?", "answer": "추가 저장 공간과 삽입·삭제·인덱스 열 변경에 따른 인덱스 유지 비용을 확인합니다.", "explanation": "핵심 조회의 이득과 실제 쓰기 작업량을 비교합니다. 측정 환경과 대표 데이터가 판단 근거에 포함되어야 합니다."},
        ],
        "sources": copy.deepcopy(_SOURCES),
        "follow_ups": [
            {"topic": "PostgreSQL Index-Only Scan과 Visibility Map", "reason": "인덱스에서 읽기를 끝낼 수 있는 조건과 MVCC가 테이블 접근에 미치는 영향을 이해합니다."},
            {"topic": "PostgreSQL 인덱스 유지 비용과 운영 모니터링", "reason": "읽기 이득뿐 아니라 쓰기 비용과 실제 사용량을 운영 지표로 평가합니다."},
        ],
    },
    2: {
        "schema_version": 1, "title": "복합 인덱스와 EXPLAIN으로 검증하기", "minutes": 40,
        "sections": [
            {"kind": "concept", "title": "같은 열도 순서에 따라 역할이 다르다 · 8분",
             "body_markdown": "복합 인덱스는 여러 열의 값을 순서대로 묶어 정렬합니다. `(customer_id, created_at)`에서는 먼저 고객별 범위가 나뉘고 각 고객 범위 안에서 생성 시각이 정렬됩니다. 따라서 고객 동등 조건과 생성 시각 범위/정렬을 같이 사용하는 조회의 후보가 됩니다.\n\n열 순서는 실제 조회 조건과 정렬을 바탕으로 판단합니다. ‘선택도가 큰 열을 무조건 먼저’처럼 하나의 규칙만 적용하지 마세요. 선행 열의 동등 조건, 다음 열의 범위 조건, 반환 행 수와 정렬 요구가 함께 중요합니다. 최적화 기법과 버전별 차이는 공식 문서와 실제 계획으로 확인합니다.",
             "visual_ids": [], "source_ids": ["s1"]},
            {"kind": "mechanism", "title": "고객 범위 안의 시간 순서를 이용한다 · 10분",
             "body_markdown": "그림 v1은 `customer_id`가 먼저인 정렬을 보여 줍니다. `customer_id = 42`로 하나의 고객 범위를 고르면 그 안에서 시간 순서를 이용할 수 있습니다. `created_at`만 지정했을 때와는 탐색 조건이 다릅니다.\n\n정렬 방향, NULL 위치, 추가 조건, LIMIT, 선택한 열과 통계에 따라 실제 계획은 달라집니다. 인덱스 순서를 활용해 별도 정렬을 피할 가능성을 검토하되, SQL과 실행 계획에서 `Sort` 유무 및 실제 비용을 확인하세요.\n\n활동: 종이에 `(created_at, customer_id)` 순서로 표를 다시 그려 보세요. 고객 42의 데이터가 한 범위로 모이는지, `created_at`만으로 범위를 고르는 일이 어떤 점에서 쉬워지는지 비교합니다. 같은 인덱스 하나가 모든 쿼리에 최선일 필요는 없습니다.",
             "visual_ids": ["v1"], "source_ids": ["s1", "s2"]},
            {"kind": "example", "title": "최근 주문 20개: 추정과 실제를 비교한다 · 15분",
             "body_markdown": "이 SQL은 첫 파트의 연습 테이블을 사용합니다. **EXPLAIN ANALYZE는 SELECT를 실제 실행합니다.** 큰 운영 조회에 바로 사용하지 말고 대표 규모의 별도 실습 데이터에서 비교하세요.\n\n```sql\nCREATE INDEX orders_customer_created_idx\nON orders (customer_id, created_at DESC);\n\nANALYZE orders;\nEXPLAIN (ANALYZE, BUFFERS)\nSELECT id, created_at, total_cents\nFROM orders\nWHERE customer_id = 42\nORDER BY created_at DESC\nLIMIT 20;\n```\n\n① Scan 종류와 인덱스 이름을 읽습니다. ② 추정 `rows`와 실제 `actual rows`를 비교합니다. ③ `loops`가 있다면 노드 실행 횟수도 함께 해석합니다. ④ `Sort` 유무와 `Buffers`를 확인합니다. 실행 시간은 한 번의 결과만으로 결론 내리지 말고 캐시·동시 부하 등 조건을 적어 반복 비교하세요.\n\n다음 5분은 고객 데이터가 한쪽으로 몰리는 경우를 예상합니다. 추정과 실제 행 수가 크게 다르면 통계나 데이터 분포가 계획에 주는 영향을 먼저 살펴보세요. 이후 5분은 `(customer_id)` 단일 인덱스와 복합 인덱스의 조회 이득·공간·쓰기 비용을 표로 비교합니다. 마지막 5분은 필요한 인덱스 후보 하나와 그 판단 근거를 메모합니다.",
             "visual_ids": [], "source_ids": ["s2", "s3"]},
            {"kind": "summary", "title": "유한한 기본 과정의 마무리 · 7분",
             "body_markdown": "먼저 실제 쿼리의 조건·정렬·LIMIT을 적고 열 순서 후보를 만듭니다. 그다음 대표 데이터에서 실행 계획과 시간을 비교하며 추정과 실제의 차이를 확인합니다. 읽기 이득이 쓰기·공간 비용을 정당화하는지도 검토합니다.\n\n이 두 파트의 기본 과정은 여기서 완료합니다. 퀴즈를 풀고 필요하면 본문을 복습하세요. 심화 후보를 선택할 때만 새로운 과정 초안을 만듭니다. 선택하지 않아도 이 과정의 완료 상태는 유지됩니다.",
             "visual_ids": [], "source_ids": ["s1", "s3"]},
        ],
        "visuals": [
            {"id": "v1", "kind": "ascii", "source": _PREFIX, "caption": "v1. customer_id가 앞에 있으면 고객별 범위가 먼저 모이고, 그 범위 안에서 created_at 순서를 갖습니다. row A~D는 서로 다른 주문 행입니다.", "alt_text": "고객 7과 고객 42의 범위가 나뉘며 각 범위 안에서 날짜가 정렬된 복합 인덱스", "fallbacks": []},
        ],
        "quizzes": [
            {"id": "q1", "question": "고객별 최근 주문 20개를 읽는 쿼리에 `(customer_id, created_at DESC)`를 후보로 삼는 이유는 무엇인가요?", "answer": "고객 동등 조건으로 범위를 좁힌 뒤 그 범위 안의 생성 시각 역순을 사용할 가능성이 있기 때문입니다.", "explanation": "인덱스 존재만으로 최적 계획을 보장하지 않습니다. 실제 Scan, Sort, 반환 행 수와 비용을 확인해야 합니다."},
            {"id": "q2", "question": "EXPLAIN의 추정 rows와 EXPLAIN ANALYZE의 actual rows가 크게 다르면 무엇을 확인하겠습니까?", "answer": "통계의 상태, 실제 데이터 분포, 조회 조건을 먼저 확인합니다. 노드의 loops와 측정 환경도 함께 해석합니다.", "explanation": "추정이 부정확하면 계획 선택에 영향을 줄 수 있습니다. EXPLAIN ANALYZE는 실제 실행하므로 안전한 실습 환경에서 사용합니다."},
        ],
        "sources": [
            {"id": "s1", "title": "PostgreSQL: Multicolumn Indexes", "url": "https://www.postgresql.org/docs/current/indexes-multicolumn.html", "checked_at": None},
            {"id": "s2", "title": "PostgreSQL: Indexes and ORDER BY", "url": "https://www.postgresql.org/docs/current/indexes-ordering.html", "checked_at": None},
            copy.deepcopy(_SOURCES[2]),
        ],
        "follow_ups": [
            {"topic": "PostgreSQL 쿼리 통계와 추정 행 수", "reason": "통계와 데이터 분포가 실행 계획 선택에 영향을 주는 원리를 더 깊이 학습합니다."},
            {"topic": "PostgreSQL 부분 인덱스와 표현식 인덱스", "reason": "전체 데이터 인덱스가 맞지 않는 조건과 계산값 조회를 구체적인 후보로 설계합니다."},
        ],
    },
}


class DemoGenerator:
    """No subprocess or network; always serves the labelled fixture course."""

    prompt_version = "demo-1"
    search_mode = "cached"
    cli_version = "offline-demo"
    model = None
    reported_model = None

    async def generate_plan(self, topic: str, feedback=None, previous=None) -> dict:
        return validate_plan(copy.deepcopy(DEMO_PLAN))

    async def generate_lesson(self, plan: dict, ordinal: int, prior_summary: str = "") -> dict:
        return validate_lesson(copy.deepcopy(DEMO_LESSONS[ordinal]))
