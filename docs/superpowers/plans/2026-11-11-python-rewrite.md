# Python 독립 배포 구현 계획

> **작업자 안내:** `executing-plans`로 구현한다. 독립된 모듈은 `dispatching-parallel-agents`로 나누고 마지막에 전체 변경을 리뷰한다. 전역 지침에 따라 구현 후 테스트와 수정 순서로 실행한다.

**목표:** 같은 Python Core를 포함한 Bazarr+ 플러그인과 런타임 포함 CLI 6종을 배포한다.

**구조:** 동기 표준 라이브러리 HTTP와 제한된 thread pool을 사용한다. Core가 식별, 탐색, 후보 및 bytes를 제공하고 CLI만 파일을 쓴다.

**기술:** Python 3.12~3.14, Beautiful Soup, unittest, PyInstaller.

**설계:** `docs/superpowers/specs/2026-11-11-python-standalone-design.md`

## 공통 제약

- DB와 별도 서비스가 없다. CLI와 플러그인이 각자 Core를 포함한다.
- 요청 최대 4개, 호스트 시작 간격 1초, 응답 4 MiB, 다운로드 100 MiB, 자막 10 MiB, 재사용 메모리 128 MiB.
- URL, DNS, redirect와 실제 연결을 검증하고 TLS 인증서 검증을 유지한다.
- 작품별 예외 없이 제목, 시즌, 회차 및 출처 근거로 판정한다.
- 문서와 보고는 한국어, 코드와 테스트 이름은 영어로 쓴다.
- 기존 C#과 데이터는 검증 전까지 보존한다.

## 리뷰 초점

- 숫자 제목과 시즌 구두점이 섞인 입력: 다른 작품을 선택하지 않는다. Task 2 및 3 테스트.
- 공유 블로그와 무관한 첨부: category 권한으로 잘못 승인하지 않는다. Task 4 테스트.
- 병렬 취소와 중복 회차 영상: 성공 파일과 각 경로를 보존한다. Task 5 테스트.
- worker 재시작 및 설정 변경: payload 재검증, 이전 설정의 cache를 쓰지 않는다. Task 6 테스트.
- 설치 파일만 있는 환경: C#, 원본 소스, 시스템 Python 없이 실행한다. Task 7 테스트.

## 파일 및 인터페이스

`src/arr_subtitle/models.py`: frozen dataclass `Anime(anime_id:int,title:str,original_title:str='',titles:tuple[str,...]=(),season:int|None=None)`, `Caption(episode:str,website:str,name:str='',updated_at:str='')`, `Blog(url:str,platform:str,id:str)`, `Post(url:str,title:str,html:str,labels:tuple[str,...]=(),summary:bool=False,category_url:str|None=None)`, `Attachment(url:str,filename:str,page:str)`, `Candidate(anime:Anime,episode:str,maker:str,page:str,attachment:Attachment,member:str|None=None,source_episode:str|None=None,numbering_evidence:str|None=None)`. `CollectorError(status,message)`, `Cancelled`, `Budget(timeout=60,cancel:Event|None=None)`의 `check()`, `remaining`, `cancelled`를 사용한다. 회차는 정규화 문자열과 Decimal 비교다.

`settings.py`: `Settings(anilist_enabled=True,blogs=tuple(),mappings=tuple())`, `Mapping(title,season,anime_id,aliases=tuple(),library_path=None,episode_offset=0)`, `parse_settings(dict)->Settings`, JSON camelCase 변환. `serialization.py`: 모델을 camelCase JSON으로 변환한다.

`network.py`: `Response(url:str,data:bytes,content_type:str='',filename:str|None=None)`, `Session(transport=None,interval=1.0)`의 `get(url,budget,limit=4194304,scope=None)->Response`, `post_json(url,body:dict,budget)->Response`, `counts`, `received_bytes`, `close()`. 테스트 transport는 `(url,method,body,timeout,limit)->Response`이며 실제 네트워크 대신 이 경계만 교체한다. `validate_url`, `public_address`, `redact`.

`identity.py`: `normalize`, `equal`, `mentions`, `explicit_season`, `select_candidates`, `parse_media(path)->(query:dict,episode:str)`, `Resolver(session,settings).resolve(query,budget)->Anime`, `Anissia(session).search(query,budget)`, `.detail(id,budget)`, `.captions(id,budget)`, `metadata_titles(session,title,budget)`.

`blogs.py`: `known_blog(url)->Blog|None`, `detect_blog(session,url,budget)->Blog`, `in_scope(blog,url)->bool`, `Explorer(session)`의 `read(url,budget,blog=None)->Post`, `explore(blog,titles,budget,categories=(),exhaustive=True)->Iterator[Post]`, `complete`.

`subtitles.py`: `episode(value)->str`, `episodes(text,require_label=False)->tuple[str,...]`, `post_episodes(title,anime)`, `extract(post)->tuple[Attachment,...]`, `attachment_title(name)`, `validate_identity(name,anime,episode)`, `validate(data,extension)->(text,encoding)`, `archive_entries(data)->list[(name,bytes)]`, `inspect(session,attachment,post_numbers,budget)->list[(name,numbers,member)]`, `payload(session,candidate,budget)->(bytes,extension,member)`. `numbering.py`: 출처별 `Numbering(offset,maximum,evidence)`와 caption 대응 및 충돌 registry.

`engine.py`: `Engine(session,settings).search(query,budget,progress=None)->dict`, `.candidates(anime,budget,progress=None)->(list[Candidate],list[dict],bool,set[str])`, `.collect(query,targets:list[dict],output,budget,search_only=False,progress=None)->dict`. target는 episode와 mediaPath다. `storage.py`: 기존 자막 확인, 원자적 no-clobber 저장, 설정 잠금 갱신과 manifest.

`cli.py`: `main(argv=None)->int`, `batch.py`: bounded 폴더 순회와 작품별 요청 공유. `integrations/bazarr_plus/provider.py`: `ArrSubtitleProvider.search(video,languages,config)` 및 `.download(provider_payload,language,config)`.

## Task 1: 모델과 실행 환경

- [x] 모델, budget, 설정, serialization과 pyproject 및 정확 의존성 lock 구현.
- [x] Python 안정 런타임의 프로젝트 venv 생성. 시스템 Python 설치를 변경하지 않는다.
- [x] `tests/test_models.py`에 회차 및 camelCase, invalid settings, deadline 검증.
- [x] `python -m unittest discover -s tests -v`: Task 1 테스트 통과.

## Task 2: 네트워크와 작품 식별

- [x] network.py와 identity.py 구현. DNS 결과를 고정한 실제 socket 연결과 hostname TLS 검증.
- [x] `tests/test_network.py`, `tests/test_identity.py`: 사설망, redirect, duplicate in-flight, 부분 제목, NFO 충돌, 시즌, 메타데이터 fallback 검증.
- [x] 같은 세션의 병렬 호출이 요청과 bytes를 공유하는지 검증.

## Task 3: 블로그와 자막

- [x] blogs.py, subtitles.py, numbering.py 구현.
- [x] `tests/test_blogs.py`, `tests/test_subtitles.py`: 피드 완전성, 반복 페이지, scope, category, ZIP 안전성, SAMI, 인코딩, 숫자 제목 및 누적 대응 검증.
- [x] 단일 무회차 파일 상속과 복수 회차의 모호성 거부 검증.

## Task 4: 공통 검색과 수집

- [x] engine.py 구현. 발견과 download 모두 같은 candidate identity 판정을 사용한다.
- [x] 출처별 오프셋 충돌, 별칭 격리, missing 범위와 부분 실패 보존.
- [x] `tests/test_engine.py`: direct, batch, cumulative, unrelated aliases, multiple titles and declared gaps.
- [x] 기존 C# 테스트 이름과 Python 대응 테스트 목록을 `docs/python-parity.md`에 기록.

## Task 5: CLI와 폴더 병렬 다운로드

- [x] storage.py, batch.py, cli.py 구현. 기존 주요 명령과 bare path 지원.
- [x] 동시 파일 저장, 기존 자막 보호, 설정 optimistic lock, cancellation manifest.
- [x] `tests/test_cli.py`, `tests/test_storage.py`, `tests/test_batch.py`: 중복 회차 파일, 여러 작품, symlink, 경로만 입력, JSON 출력과 종료 코드.
- [x] 전체 Python 테스트 실행.

## Task 6: Bazarr+ 독립 플러그인

- [x] provider.py와 template manifest 구현. Core 복사, 파일 해시 및 catalog 생성은 build script에서 수행.
- [x] fallback_blogs, mappings_json, timeout 및 AniList 설정 validation.
- [x] 안정 후보 id, 재시작 가능한 payload, 한국어와 HI 및 forced 필터, SAMI format과 no filesystem writes.
- [x] `tests/test_provider.py`, `tests/test_bundle.py`: 격리된 bundle search/download, 설정 변경, tampered payload, hash 검증.
- [x] 고정 upstream SDK 및 worker의 실제 계약 검증.

## Task 7: 6개 CLI 배포와 전환

- [x] scripts/build_release.py 및 PyInstaller 설정, Python 3.12~3.14 테스트와 6개 네이티브 빌드 workflow.
- [ ] 아카이브 단독 smoke, TLS 정상 및 잘못된 인증서 거부, Python 및 .NET 없는 환경 확인.
- [x] README 및 CLI, 검색, 플러그인 설치 문서 갱신. 기존 설정 호환 문서화.
- [x] C# 동작 조건 대응과 Python 전체 회귀, 별도 전체 변경 리뷰 및 수정.
- [ ] 검증 후에만 C# 제품 코드와 전용 빌드 구성 제거.
- [ ] v0.2.0 배포물 7개와 SHA256SUMS 검증. 실패 대상이 있으면 Release 공개하지 않는다.

## 진행 기록

설계 승인 후 계획 작성. 사용자의 진행 지시에 따라 구현을 이어간다. 실행은 이 세션에서 수행하며 독립 모듈만 병렬 위임한다. TDD 형식보다 구현 후 검증을 우선하는 전역 지침을 적용한다.

### 중간 검증과 판단

- 모델, 네트워크, 식별, 블로그, 자막, 엔진, CLI, 플러그인 및 패키징 구현을 작성했다. C# 소스는 아직 비교 기준으로 남아 있다.
- Python 전체 테스트 233개 통과 후 별도 리뷰를 진행했다. 출처 정규화, 개별 영상 메타데이터, 매핑 전달, caption 번호 체계, 끝 회차 누락, 헤더 deadline, 응답 메모리 전환과 짧은 Content-Length를 수정했다. 최종 전체 테스트 266개를 통과했다.
- 실제 흉란영애 1~6화 검색, 정확 제목의 1~13화 검색과 누적 다운로드를 확인했다.
- 선택 후보 다운로드가 전체 블로그를 다시 순회해 예산을 소진하므로 선택 출처와 해당 caption만 재검증하도록 변경했다. 실제 플러그인 1화 다운로드는 5.556초에 성공하고 64,858 bytes의 SHA-256이 일치했다.
- Linux x64 runtime 포함 CLI를 빌드했다. Python, .NET 및 호스트 OpenSSL 없는 Ubuntu 컨테이너의 fixture와 TLS smoke를 통과했다.
- 리뷰의 수집 성공 기준 변경 제안은 적용하지 않는다. `collect` 성공은 요청한 파일 확보이며 `search`의 전체 탐색 성공과 다르다. 기존 유효 자막이 있으면 네트워크 탐색 없이 보존한다. 이 구분이 틀리면 사용자가 수집 성공을 전체 출처 조사 완료로 오해할 수 있어 문서에 명시한다.
- 카테고리와 누적 회차만으로 낯선 작품명을 별칭으로 승격하지 않는다. C#의 일부 약칭 자동 승인이 더 이상 통과하지 않을 수 있다. 실제 작품 근거가 없는 약칭은 명시 매핑으로 처리한다.
- 동일한 title-based 출력 stem 충돌은 작품 번호를 이름에 포함해 방지한다. 영상 옆 sidecar 이름은 기존대로 유지한다.
- 커뮤니티 카탈로그는 별도 `provider-catalog` 브랜치에 실제 Core 파일을 먼저 게시하고 그 commit을 manifest가 참조하도록 한다. Release ZIP의 내부 해시만으로 원격 설치 성공을 주장하지 않는다.
- 리뷰의 category 별칭 승격은 제거했다. 기존 허용보다 보수적이므로 낯선 작품 약칭은 명시 매핑이 필요할 수 있다.
- Windows의 악의적인 동시 junction 교체를 kernel handle로 방어하는 것은 구현하지 않았다. 경로 재검사와 no-clobber는 유지하며 실제 Windows 동작은 CI로 검증한다. 공격자가 쓰기 권한을 가진 부모 디렉터리를 실행 중 교체하는 환경에서는 경로 보호가 완전하지 않을 수 있다.
- C# 제거는 6개 대상 Python 배포 동등성 결과를 확인한 뒤 수행한다. 로컬 Linux 결과만으로 원래 코드를 제거하지 않는다.
- 첫 CI는 YAML의 colon-containing plain command 때문에 job 시작 전 실패했다. block scalar로 수정하고 원격 검사를 다시 시작했다.
- 원격 Windows x64 및 ARM64에서 frozen 도움말의 한국어가 CP1252 stdout에 기록되지 않는 실패를 확인했다. 실행 진입점의 stdout과 stderr를 UTF-8로 설정하고 회귀 테스트를 추가했다. 수정 후 두 Windows 네이티브 smoke가 통과했다. 전체 Python 테스트도 6개 네이티브 러너에서 실행하도록 확장했다.
