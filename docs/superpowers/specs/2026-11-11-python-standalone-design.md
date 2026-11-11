# Python 재작성과 독립 배포 설계

## 상태와 요구

설계 승인 대기다. 제품 구현은 이 문서 승인 후 시작한다.

별도 구현 관점 리뷰를 마쳤다. 계획을 막는 추가 질문이나 요구 충돌은 지적되지 않았다.

사용자는 프로젝트 전체를 Python으로 재작성하고 Bazarr+ 플러그인과 독립 CLI를 각각 배포하도록 요청했다. 두 배포물은 각자 Core를 포함하며 서로를 설치하거나 별도 HTTP 서비스를 실행할 필요가 없어야 한다. CLI는 Python을 별도로 설치하지 않고 실행한다. 기존 Linux, macOS 및 Windows의 x64와 ARM64를 유지한다. 폴더 경로만 입력해 영상 회차에 맞는 자막을 제한된 병렬 처리로 받는다.

## 접근 방식

검토한 방식은 다음과 같다.

1. 하나의 Python Core 소스를 두 배포물에 포함한다. CLI와 플러그인은 각자 실행하며 수정은 같은 소스에 적용한다.
2. CLI와 플러그인의 Core를 별도 개발한다. 배포는 독립적이지만 회차 판정, 보안 수정과 테스트가 갈라진다.
3. Python 연결 코드가 C# 실행 파일이나 서비스에 의존한다. 사용자의 전체 Python 재작성과 독립 배포 요구에 맞지 않는다.

대화에서 제안한 1번을 기준으로 설계했다. 사용자에게 두 배포물과 런타임 포함 CLI 방향을 설명했고 사용자가 동의했다. 내부 패키지 구조와 빌드 도구는 작성자의 제안이다.

## 범위

- Anissia 작품 목록 및 caption, AniList를 통한 영문 및 로마자 제목 식별
- 명시 매핑, 로컬 NFO 및 JSON 근거, 작품 제목과 시즌 충돌 검사
- 제작자 출처와 URL로 등록한 전역 fallback 블로그
- Blogspot, 티스토리, 네이버 및 일반 블로그의 HTTP 탐색
- 직접 자막과 ZIP, 회차 범위, 누적 번호 및 실제 파일명 근거
- 정확 제목 우선, 부분 검색, 구두점으로 구분되는 제목 보존
- CLI 검색, 매칭, 수집, 폴더 병렬 처리, 설정과 결과 manifest
- Bazarr+ Provider Hub 설치 패키지와 커뮤니티 카탈로그
- 패키징, 6개 플랫폼 실행 검증과 체크섬 포함 Release

SQLite, 영속 색인, 상시 감시, 새 HTTP 서비스, 공식 Bazarr 어댑터, 로그인 및 CAPTCHA 우회, 브라우저 자동화, 영상 다운로드, 번역과 자동 싱크는 제외한다. RAR, 7z, 중첩 압축과 다중 회차 영상은 이번에도 지원하지 않는다. 영화 지원은 추가하지 않는다.

## 소스와 모듈

`src/arr_subtitle/`를 단일 Python 패키지로 둔다. Core는 CLI나 Bazarr 내부 모듈을 import하지 않는다.

- 모델: 작품, 시즌, 회차, 게시물, 첨부, 검색 후보, 출처 시도, 결과와 오류
- 네트워크: 주소 정책, TLS, 연결, 응답 제한, 중복 요청 공유, 속도 제한과 취소
- 식별: Anissia, AniList, 경로와 로컬 메타데이터, 검증된 별칭
- 블로그: 플랫폼별 본문 및 목록 파싱, 페이지 순회와 완료 근거
- 자막: 회차 및 제목 파싱, 누적 매핑, 첨부 검사, ZIP 선택과 본문 검증
- 수집: 발견, 판정, 요청 회차 충족과 전체 검색 조정
- 저장: CLI 전용 sidecar와 설정 및 manifest의 안전한 쓰기

`integrations/bazarr_plus/provider.py`는 Provider Hub의 plain dict 계약을 Core에 연결한다. CLI는 `src/arr_subtitle/cli.py`와 패키지 진입점에 둔다. 패키징은 소스 파일을 명시적으로 복사하며 빌드 결과를 소스에 되돌려 쓰지 않는다.

CLI와 플러그인의 판정 경로는 같은 Core를 사용한다. C#판의 검색과 수집에 남아 있는 서로 다른 약칭 허용 규칙을 그대로 복제하지 않는다. 작품명, 제작자, 릴리즈 그룹별 예외를 만들지 않는다.

## Python과 의존성

Core와 플러그인은 Python 3.12부터 3.14까지를 지원한다. CLI 빌드는 안정 버전 Python 3.14의 고정 패치 버전으로 수행한다. 로컬 기본 Python 3.15 RC를 지원 기준으로 삼지 않는다.

동시성, HTTP 연결, TLS, JSON, ZIP, 인코딩, 해시와 CLI는 표준 라이브러리를 우선한다. 실제 블로그 HTML 파싱에는 Beautiful Soup의 `html.parser` backend를 사용한다. 잘못된 HTML에 대한 트리 처리와 selector를 직접 재구현하지 않기 위한 의존성이다. Beautiful Soup과 전이 의존성은 정확 버전과 wheel 해시로 고정한다. Bazarr+ manifest에도 동일하게 선언한다. 런타임 의존성은 bundle에 vendoring하지 않는다.

CLI 패키징 도구는 PyInstaller를 제안한다. 각 OS와 CPU의 네이티브 러너에서 빌드한다. Windows ARM64를 x64 에뮬레이션 결과로 대체하지 않는다. 도구 버전도 고정한다.

## 두 독립 배포물

### Bazarr+ 플러그인

provider id는 `arr_subtitle`이다. `bazarr.provider-hub.v1`을 사용한다. 배포 ZIP 안에는 `provider.json`, 어댑터와 Core의 선언된 `.py` 파일만 둔다. 모든 Python 파일과 bundle의 SHA-256을 생성한다. CLI, C# 바이너리, 로컬 설정, 테스트 데이터와 네이티브 파일을 포함하지 않는다.

플러그인은 Bazarr+ worker의 Python과 manifest로 설치한 의존성을 사용한다. 따라서 독립이라는 말은 다른 arr-subtitle 배포물이나 Core 서비스가 필요 없다는 뜻이다. Bazarr+ 자체의 Python worker까지 플러그인에 포함한다는 뜻은 아니다.

로컬 ZIP 설치와 이 저장소의 커뮤니티 카탈로그를 제공한다. 공식 카탈로그 등재나 upstream PR은 이번 Release의 조건이 아니다. 로컬 및 커뮤니티 패키지는 untrusted로 표시되는 정상 동작을 문서화한다. 기존 provider id를 덮어쓰지 않는다.

### 독립 CLI

6개 배포물은 Python 런타임, Core, CLI와 필요한 라이브러리를 함께 포함한다. 기본은 PyInstaller onedir 디렉터리를 압축하는 방식이다. 압축을 풀고 실행 파일을 호출하면 된다. 일회성 임시 디렉터리 추출이 필요한 onefile 형태는 요구하지 않는다.

Linux 및 macOS는 tar.gz, Windows는 ZIP을 사용한다. Linux는 glibc 기준이다. Alpine의 musl은 추가하지 않는다. Linux TLS 라이브러리를 번들에 포함해 호스트 libssl 3 및 LD_LIBRARY_PATH 의존을 없앤다. 시스템 CA와 OS 기본 라이브러리는 필요하다. 인증서 검증을 비활성화하지 않는다. macOS와 Windows의 신뢰 저장소 선택은 해당 런타임 동작을 검사해 문서화한다.

같은 Release에 플러그인 ZIP 하나, CLI 압축 파일 6개와 SHA256SUMS를 공개한다. 이식 첫 버전은 `v0.2.0`을 제안한다. CLI 이름과 저장소 origin은 유지하고 로컬 디렉터리 이름을 자동 변경하지 않는다.

## 폴더 입력과 병렬 수집

`arr-subtitle <폴더>`는 `scan <폴더>`와 같은 다운로드 동작이다. `download <폴더>`도 받는다. 파일 경로만 입력하면 해당 영상의 다운로드로 처리한다. 기존 명령인 search, match, download, scan, collect, blogs와 주요 옵션은 유지한다.

폴더는 재귀 탐색하며 `.mkv`, `.mp4`, `.avi`를 대상으로 한다. 심볼릭 링크와 디렉터리 junction을 따라가지 않는다. 작품 루트, 작품 폴더와 Season 폴더 어디서 시작해도 각 영상의 실제 경로로 작품과 시즌을 판단한다. 하나의 anime-id를 여러 작품 전체에 강제 적용하는 모호한 입력은 거부한다.

작품과 시즌별로 메타데이터 및 출처 발견을 공유한다. 발견된 독립 첨부 다운로드와 파일 저장은 제한된 병렬 작업으로 처리한다. 같은 ZIP은 한 번 받고 여러 요청 회차에 사용한다. 같은 회차의 여러 영상 파일도 각 영상의 sidecar를 만든다. 요청을 episode 값만으로 축약해 파일 하나를 잃어버리지 않는다.

동시에 실행하는 네트워크 요청은 최대 4개다. 같은 호스트는 요청 시작 사이에 최소 1초를 둔다. 경로 처리의 병렬성과 별개로 이 제한은 세션 전체에서 적용한다. 큐와 실행 중 응답의 총 메모리도 제한한다. 무제한 future 생성이나 파일 전체 목록을 한꺼번에 적재하지 않는다.

영상 옆에 원래 파일명과 같은 stem의 `.ko.<확장자>`를 쓴다. 기존 유효한 `.ko` 및 `.kor` 자막은 보존한다. 잘못된 기존 파일도 자동 삭제하거나 덮어쓰지 않고 오류로 보고한다. 한 회차 실패가 다른 회차 작업을 취소하지 않는다. Ctrl+C는 신규 작업을 멈추고 성공한 결과와 미완료 대상을 보고한 뒤 130으로 종료한다.

stderr에는 완료한 파일, 발견 결과와 실패를 즉시 표시한다. stdout은 텍스트 결과 또는 JSON 문서 하나다. JSON과 기존 설정의 camelCase 필드 및 주요 상태 이름은 유지한다. 영상 식별 결과마다 mediaPath와 저장 경로를 보존해 여러 작품의 동일 회차를 구분한다. 실행 전체 elapsed 시간은 병렬 작업 시간의 합이 아니라 실제 경과 시간이다.

## Bazarr+ 계약

`search(video, languages, config)`는 요청된 한국어 일반 자막만 검색한다. `kind=episode`를 지원하며 forced 또는 HI 전용 요청에 일반 자막을 해당 속성이 검증된 것처럼 반환하지 않는다. 영상 경로가 없어도 series, season과 episode를 사용한다. TVDB 또는 IMDb 번호를 Anissia 번호로 취급하지 않는다. 제목과 시즌을 확정할 수 없으면 명시 매핑을 요구한다.

결과 후보는 검증된 작품 및 회차, 제작자, 게시물, 첨부 또는 archive member를 구별한다. id는 동일 후보에서 안정적이다. `matches`는 실제로 확인한 series, season, episode 및 릴리즈 정보만 포함한다. 해시, release group, fps나 HI를 추측해 점수를 올리지 않는다. `hash_verifiable`은 false다. 후보 payload는 엄격히 검증하며 다음 호출이나 worker 재시작 뒤에도 재구성할 수 있어야 한다.

`download(provider_payload, language, config)`는 선택한 후보만 다시 확인하고 검증된 자막 bytes를 반환한다. 영상 경로나 sidecar를 쓰지 않는다. `content_b64`, `content_sha256`, `format`과 `encoding`을 채운다. SMI는 Bazarr+의 SAMI 형식 식별과 맞춘다. 초기 버전은 원본 SMI, SRT, ASS와 SSA를 유지하며 새 변환 기능은 추가하지 않는다.

플러그인은 worker stdout에 로그나 CLI JSON을 쓰지 않는다. 로그는 stderr로 보낸다. 설정, 네트워크 실패나 식별 모호성을 빈 후보 목록으로 숨기지 않는다. 일부 출처 실패 후 유효 후보가 있으면 후보를 반환하고 제한된 경고를 stderr에 남긴다. 유효 후보가 없고 필수 탐색이 실패했으면 예외를 발생시킨다. Provider Hub가 임의의 검색 완료 상태나 오류 event를 보여준다고 가정하지 않는다. CLI의 전체 검색 보고서는 별도로 더 자세한 상태를 유지한다.

`fallback_blogs`는 한 줄에 URL 하나를 받는 문자열 설정이다. `mappings_json`은 기존 mappings와 같은 형식의 배열 문자열로 받는다. AniList 사용 여부와 제한 시간도 설정으로 전달한다. 설정은 CLI 파일을 읽지 않으며 변경 시 이전 요청의 설정과 캐시가 섞이지 않게 한다.

플러그인 내부 예산은 기본 20초, 개별 HTTP 요청은 최대 10초로 둔다. 호스트 worker deadline은 어댑터 search에 전달되지 않으므로 이를 읽을 수 있다고 가정하지 않는다. Bazarr+의 실제 worker 예산과 함께 설치 smoke에서 확인한다. 장기 영속 캐시나 DB는 추가하지 않으며 worker 내 짧은 응답 재사용은 시간, 크기 및 설정 해시로 제한한다.

## 공통 검색과 안전성

전체 제목 정확 일치가 있으면 해당 작품만 사용한다. 그렇지 않을 때는 실제 제목 또는 검증된 별칭의 부분 일치를 허용하며 구두점으로 구별되는 제목 경계를 유지한다. 작품별 조건문, 제작자 약칭 목록과 릴리즈 그룹 목록을 넣지 않는다.

게시물 재조회 뒤 제목과 시즌을 다시 검사한다. 카테고리는 탐색 범위 근거이지 그 안의 모든 첨부를 다른 작품의 별칭으로 승인하는 권한이 아니다. 누적 번호는 caption, 게시물과 개별 첨부의 대응 근거로 확정하고 출처와 카테고리별로 격리한다. 충돌한 오프셋으로 만든 결과는 무효화한다.

ZIP의 회차 없는 자막 하나는 유일한 게시물 또는 caption 회차만 상속한다. 게시물의 명시 범위와 실제 첨부 회차 차이를 보고한다. 단독 지정 회차 작품은 지정 범위 밖을 누락으로 만들지 않는다. 숫자 제목과 해상도, 시즌 토큰을 회차로 오인하지 않는다. 식별 근거와 출처 실패를 결과에 보존한다.

외부 URL은 http 및 https의 80, 443 포트만 허용한다. 사용자 정보, 사설 IP와 비공개 DNS 결과를 차단한다. 각 리다이렉트와 연결 시점에 검사하며 DNS 검사 뒤 새 DNS 조회로 연결하는 rebinding 빈틈을 만들지 않는다. 블로그 순회는 등록한 출처 범위를 벗어나지 않는다. 공개 첨부 저장소 다운로드는 허용한다. 환경 proxy를 자동 사용하지 않는다.

설정 및 로컬 메타데이터는 1 MiB, HTML 및 API 응답은 4 MiB, 다운로드 ZIP은 100 MiB, 선택 자막은 10 MiB, 응답 재사용 총량은 128 MiB를 상한으로 둔다. ZIP은 최대 500개 멤버 및 총 확장 100 MiB로 제한한다. 경로 탈출, 절대 경로, 중복 정규화 경로, symlink와 중첩 압축을 거부한다. XML은 DTD와 외부 엔티티를 금지하고 크기 및 깊이를 제한한다. 자막은 UTF-8, UTF-16과 CP949 등을 엄격히 판정하고 HTML 오류 페이지를 거부한다.

쓰기는 같은 디렉터리의 임시 파일에 기록하고 flush 및 fsync 뒤 원자적으로 게시한다. 새 sidecar는 기존 파일을 덮어쓰지 않는다. 설정 갱신은 잠금과 기존 내용 해시로 동시 변경을 검사한다. 취소와 쓰기 실패를 숨기지 않는다. 파일명과 URL, provider payload를 통한 shell 실행은 없다. UI 설정 필드는 제목과 설명을 제공하고 색상만으로 의미를 전달하지 않는다.

## 검증과 전환

기존 C# `v0.1.2`의 147개 테스트가 담은 동작을 목록으로 만들고 Python 테스트와 대응시킨다. 테스트 개수 자체보다 모든 판정 조건의 이식 여부를 확인한다. 알려진 C# 결함은 정답으로 복제하지 않는다.

네트워크 fixture는 Anissia, AniList, 블로그 페이지 순회, Drive 리다이렉트, 파일명 헤더, 직접 자막과 ZIP을 다룬다. 단위 및 통합 테스트는 실제 외부 사이트를 호출하지 않는다. 병렬 테스트는 지연 대신 동기화 장치와 제어 가능한 clock을 사용해 중복 요청, 호스트 간격, 취소, 덮어쓰기 경쟁과 메모리 상한을 검증한다.

플러그인은 Python 3.12, 3.13, 3.14에서 manifest 검증 및 실제 Provider Hub worker 계약 smoke를 실행한다. ZIP을 별도 임시 디렉터리에 풀고 저장소의 Core, CLI, C#과 PYTHONPATH 없이 search 및 download를 검증한다. SMI가 실제 Bazarr+ host에서 인식되는지 검사한다. 잠근 SDK와 호스트 revision을 기록하며 최신 미고정 원격 코드를 CI에서 임의 실행하지 않는다.

CLI는 6개 대상에서 압축을 푼 배포물만으로 도움말, 버전, 직접 및 합본 다운로드, 전체 검색, 취소와 폴더 병렬 처리 smoke를 수행한다. 호스트 Python이나 .NET이 없어도 실행되는지 검사한다. Linux는 별도 컨테이너에서 TLS 검증과 잘못된 인증서 거부를 확인한다. macOS와 Windows의 실제 ARM64 및 x64 아키텍처도 검사한다.

실제 외부 출처 검증은 수동 실행으로 분리한다. 흉란영애, 정확 제목 검색, Working의 시즌 경계와 누적 회차를 확인한다. 실패한 출처는 성공 처리하지 않는다. Release는 6개 CLI와 플러그인 검사가 모두 통과해야 공개한다.

이식 중 C# 소스와 테스트는 비교 기준으로 보존한다. Python 기능 및 배포 동등성 검증이 끝난 뒤 C# 제품 코드, .NET 빌드 설정과 전용 OpenSSL 빌드 경로를 제거한다. 기존 Git 이력과 공개 v0.1.x Release 및 사용자 설정과 다운로드 데이터는 보존한다.

## 구현자가 결정할 항목

세부 함수 시그니처, 파일 분할, 고정 패치 버전과 wheel 해시, thread-safe 세션의 구체 구현은 구현 계획에서 확정한다. 런타임 필요 파일, CA 선택과 각 플랫폼의 최소 OS는 실제 패키징 검증 결과를 문서화한다. 이 단계에서 의존성이 늘거나 플랫폼 하나를 제외해야 하면 조용히 축소하지 말고 사용자에게 알린다.

## 참조

- https://github.com/LavX/bazarr-provider-catalog/blob/main/sdk/README.md
- https://github.com/LavX/bazarr-provider-catalog/blob/main/providers/smoke/provider.py
- https://github.com/LavX/bazarr-provider-catalog/blob/main/providers/subsarr/provider.json
- https://github.com/LavX/bazarr/blob/master/bazarr/provider_hub/worker_runner.py
- https://github.com/LavX/bazarr/blob/master/bazarr/provider_hub/protocol.py
- https://github.com/LavX/bazarr/blob/master/bazarr/provider_hub/bundle.py
- https://pyinstaller.org/en/stable/CHANGES.html
