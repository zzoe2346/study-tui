# 협업 지침

개인용 백엔드 학습 TUI를 만든다.

- 제품 요구사항과 미정 선택은 [docs/requirements.md](docs/requirements.md)를 기준으로 구분한다.
- 제품 결정이 바뀌면 해당 요구사항 문서를 갱신한다.
- 구현 구조와 데이터 계약은 [docs/lld.md](docs/lld.md)를 참조하고, 설계가 바뀌면 함께 갱신한다.

## 작업 방식

- 구현 전에 요구사항 문서의 미정 기술 선택을 사용자와 정한다.
- 기존 사용자 변경을 확인하고 보존한다. 관련 없는 작업을 덮어쓰거나 되돌리지 않는다.
- 비밀정보와 로컬 학습 데이터·생성 산출물을 커밋하지 않는다. `.gitignore`와 추적 파일을 함께 확인한다.
- 검증은 변경에 맞는 의미 있는 범위로 수행한다. 문서는 요구사항 누락·충돌, 링크, Git 상태를 확인하고, 구현에서는 변경한 동작을 검증한다. 저위험 변경에 구현을 그대로 반복하는 테스트를 추가하지 않는다.
- 사용자 Git author 설정을 유지하고 글로벌 Git 설정을 바꾸지 않는다.

## Codex 공동 작성자 표기

Codex가 기여한 커밋의 메시지 끝에 빈 줄을 두고 다음 트레일러를 넣는다.

```text
Co-authored-by: Codex <noreply@openai.com>
```

이 이름·이메일은 이 저장소의 협업 관례이며, OpenAI 공식 기본값이나 검증된 GitHub 프로필 연결 주소로 주장하지 않는다. 형식은 [GitHub 공식 공동 작성자 문서](https://docs.github.com/en/pull-requests/how-tos/commit-changes/creating-a-commit-with-multiple-authors)를 따른다.
