# Python 이식 검증

## 기준

C# `v0.1.2` 테스트의 동작 조건을 Python 테스트에 대응한다. C# 구현의 미검증 추측이나 알려진 결함을 그대로 재현하지 않는다. 현재 C# 코드는 비교용으로 남아 있다.

| C# 테스트 영역 | Python 대응 | 주요 검증 |
| --- | --- | --- |
| IdentityTests | test_identity.py | 경로, 시즌, 구두점, 영문 및 로마자 제목 |
| SearchIdentityTests | test_identity.py | 정확 제목 우선과 무관 후보 제외 |
| NetworkingTests | test_network.py | 공개 주소, scope, 요청 공유와 취소 |
| BlogTests | test_blogs.py | 네이버 wrapper, Blogspot 피드, 티스토리와 일반 목록 |
| SubtitleTests | test_subtitles.py | SAMI, SRT, ASS, 인코딩과 안전한 ZIP |
| EpisodeNumberingTests | test_subtitles.py | caption 대응, 출처 격리와 오프셋 충돌 |
| CumulativeBatchTests | test_subtitles.py, test_engine.py | 누적 회차와 ZIP member 선택 |
| GenericSearchPipelineTests | test_subtitles.py, test_engine.py | 단일 Drive 자막, 회차 없는 ZIP과 명시 누락 |
| SearchCategoryTests | test_blogs.py, test_subtitles.py, test_engine.py | category 범위와 다른 작품 첨부 거부 |
| SearchTests | test_engine.py, test_blogs.py | 과거 회차, 목록 완전성, 부분 결과 |
| SearchPerformanceTests | test_network.py, test_batch.py | 같은 요청 공유와 bounded 작업 |
| SearchPerformanceRegressionTests | test_network.py, test_batch.py | 중복 순회 및 취소 상한 |
| ReviewRegressionTests | test_identity.py, test_subtitles.py | 시즌과 회차 경계, 모호성 거부 |
| CollectionTests | test_engine.py, test_cli.py | 직접 수집과 합본, 실패 종료 |
| SettingsTests | test_models.py, test_storage.py, test_cli.py | 설정 호환, 동시 갱신과 파일 보호 |

새 배포 계약은 `test_provider.py`, `test_bundle.py`, `test_release.py`에서 별도로 검증한다. 기존 테스트 이름 수와 Python 테스트 개수가 같다는 것을 동등성 근거로 삼지 않는다.

## 확인한 실행

- Python 단위 및 통합 테스트 전체 실행 통과.
- 실제 흉란영애 검색에서 1~6화와 목록 완료를 확인했다.
- 실제 마법소녀를 동경해서 검색은 작품 2744와 1~13화를 선택했다. 네이버 실패는 미완료로 남겼다.
- 실제 작품 1975의 1화와 12화를 누적 13화 및 24화에서 다운로드했다. 저장 파일의 SHA-256이 결과와 일치한다.
- Working!! 검색은 다른 시즌을 대신 선택하지 않는다.
- bare 폴더 경로로 흉란영애 1화와 6화를 동시에 요청하고 각 영상 옆에 저장했다. 두 파일의 SHA-256이 보고서와 일치하며 공유 metadata 요청은 3개였다.
- 같은 폴더 재실행은 두 회차 모두 `AlreadyPresent`를 반환했다. 제작자 블로그와 첨부를 다시 요청하지 않았다.
- 고정 catalog SDK와 고정 Bazarr+ worker runner의 패키지 로드 및 비지원 미디어 계약을 검사했다.
- 독립 ZIP을 원본 Core 경로가 없는 프로세스에서 풀어 search 및 download를 fixture로 검증한다.

실제 Bazarr+ UI 설치, host의 SAMI 후처리와 6개 대상 빌드는 원격 또는 별도 host 검증 대상이다. 이 항목이 통과하기 전에 전체 배포 동등성을 완료로 표시하지 않는다.
