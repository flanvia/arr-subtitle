# Bazarr+ 플러그인

`arr_subtitle`은 Bazarr+ Provider Hub의 한국어 애니메이션 회차 provider다. 일반 Bazarr용 플러그인이 아니다. 영화와 HI 전용 또는 forced 전용 검색은 지원하지 않는다.

플러그인 ZIP은 어댑터와 Python Core를 포함한다. arr-subtitle CLI, .NET, 별도 Core 서버를 설치할 필요가 없다. Bazarr+ worker가 Python과 manifest의 정확 버전 의존성을 관리한다. 지원 Python은 3.12, 3.13, 3.14다.

이 변경은 새 라이선스를 선택하거나 사용 권한을 추가하지 않는다. 저장소에 `LICENSE`가 있으면 CLI 압축 파일에 포함한다. 없으면 빌드 과정에서 임의로 생성하지 않는다.

## 로컬 ZIP 설치

1. 이 저장소의 [Release](https://github.com/flanvia/arr-subtitle/releases)에서 `arr-subtitle-bazarr-plus-<버전>.zip`과 `SHA256SUMS`를 받는다. CLI용 OS 압축 파일과 혼동하지 않는다.
2. 다운로드한 ZIP의 SHA-256을 `SHA256SUMS`와 비교한다. Linux는 `sha256sum`, macOS는 `shasum -a 256`, Windows PowerShell은 `Get-FileHash -Algorithm SHA256`을 사용할 수 있다.
3. Bazarr+의 `Subtitle Hub`에서 `Marketplace`를 열고 로컬 패키지 설치를 선택한다. ZIP을 그대로 올린다. 직접 압축을 풀어 Bazarr 설치 폴더에 복사하지 않는다.
4. manifest, 파일 해시, 의존성 설치와 worker smoke 검사가 끝날 때까지 기다린다. 실패하면 `Activity`와 Bazarr+ 로그를 확인한다.
5. 변경 대기 배너가 나오면 Bazarr+를 재시작한다. 업데이트와 제거도 재시작해야 적용된다.
6. `My Providers`에서 `+`로 `arr-subtitle`을 추가하고 설정을 저장한다. 대상 언어 프로필에 한국어 일반 자막을 포함한다.

로컬 ZIP과 사용자 카탈로그는 `untrusted`로 표시된다. 이것은 해시 검증 실패가 아니라 출처 분류다. 공식 provider를 덮어쓰지 않는다. 공식 카탈로그 등재를 가정하지 않으며 `arr_subtitle`이라는 독립 id를 사용한다.

## 커뮤니티 카탈로그

검증된 Release를 공개한 뒤 `provider-catalog` 브랜치에 Core 파일과 카탈로그를 게시한다. manifest는 해당 Python 파일을 포함한 고정 commit을 참조한다. 카탈로그 URL은 다음과 같다.

```text
https://raw.githubusercontent.com/flanvia/arr-subtitle/provider-catalog/catalog.json
```

첫 Python Release 이전에는 이 주소가 없을 수 있다. URL이 조회되는지 확인한 뒤 Marketplace의 사용자 카탈로그 출처에 추가한다. 카탈로그 새로고침, 설치와 재시작 순서로 진행한다. Release ZIP만 공개하고 원격 Core 파일을 생략하는 방식은 사용하지 않는다.

화면 이름과 위치는 Bazarr+ 버전에 따라 다를 수 있다. [Provider Hub 설치 안내](https://lavx.github.io/bazarr/guides/provider-hub.html)에서 해당 버전의 로컬 패키지 및 재시작 절차를 확인한다.

## 설정

플러그인은 CLI의 `config.json`을 읽지 않는다. 아래 값은 Bazarr+ provider 설정에서 입력한다.

| 이름 | 기본값 | 의미 |
| --- | --- | --- |
| `fallback_blogs` | 빈 문자열 | 추가로 탐색할 공개 블로그 URL. 한 줄에 하나 |
| `mappings_json` | `[]` | 작품 및 시즌의 명시 매핑을 담은 JSON 배열 문자열 |
| `anilist_enabled` | `true` | AniList를 통한 영문 및 로마자 제목 식별 |
| `timeout` | `20` | 검색과 다운로드 각각의 내부 제한 시간(초). 1부터 20까지 |

### 추가 블로그

Anissia caption 출처 외에 탐색할 블로그를 등록한다.

```text
https://example.blogspot.com/
https://example.tistory.com/
https://blog.naver.com/example
```

위 주소는 입력 형식을 설명하는 예시다. 실제 제작자 주소로 바꾼다. 게시물보다 블로그 루트 주소를 권장한다. Blogspot, 티스토리, 네이버와 공개 HTML 구조가 있는 일반 블로그를 탐색한다. 로그인, CAPTCHA, 브라우저 실행이 필요한 페이지는 지원하지 않는다. 블로그 URL 등록만으로 그 안의 모든 자막을 같은 작품으로 취급하지 않는다.

공개 HTTP와 HTTPS 주소의 80, 443 포트만 허용한다. 사설 IP, localhost, 사설 DNS 결과와 허용 범위 밖 리다이렉트는 거부한다. NAS 내부 주소나 프록시를 우회 수단으로 넣지 않는다.

### 작품 매핑

제목 또는 시즌이 모호하면 `mappings_json`에 매핑을 추가한다. `animeId`는 Anissia 작품 번호다. TVDB, IMDb, AniList 번호를 넣지 않는다.

```json
[
  {
    "title": "라이브러리에 표시되는 작품명",
    "season": 2,
    "animeId": 12345,
    "aliases": ["검증한 다른 작품명"],
    "episodeOffset": 12
  }
]
```

숫자는 형식 예시이므로 실제 작품에 맞게 바꾼다. `episodeOffset: 12`는 출처 13화를 시즌 1화로 대응한다. 누적 번호를 쓰지 않으면 생략하거나 0을 쓴다. `aliases`는 같은 작품임을 확인한 제목만 넣는다. 매핑은 다른 작품이나 시즌의 첨부를 승인하는 예외 목록이 아니다.

Provider Hub는 영상 파일 경로 없이도 제목, 시즌, 회차를 전달한다. 플러그인에서는 경로 기반 `libraryPath` 매핑에 의존하지 않는다. 설정을 바꾼 뒤에는 다시 검색한다. 이전 설정에서 생성한 후보를 다운로드하면 재검색 오류가 날 수 있다.

## 검색 점수와 다운로드

후보는 검증한 `series`, `season`, `episode` 일치 근거를 반환한다. 파일 hash, fps와 release group 일치를 추측하지 않는다. `hash_verifiable`은 false다. 따라서 hash 일치를 요구하는 점수 기준에서는 검색된 후보도 자동 다운로드에서 제외될 수 있다. 최소 점수는 Bazarr+의 실제 후보 근거를 확인한 뒤 조정한다. 점수를 맞추려고 다른 작품 매핑을 넣지 않는다.

일반 한국어 자막만 반환한다. HI 또는 forced 여부를 확인한 것처럼 표시하지 않는다. 다운로드할 때 작품, 회차, 게시물, 첨부와 ZIP 멤버를 다시 검사한다. 게시물 변경이나 삭제로 이전 후보가 무효가 되면 다시 검색해야 한다.

플러그인은 검증한 자막 bytes를 Bazarr+에 반환한다. 영상 옆 파일을 직접 쓰지 않는다. 저장 위치, 파일명과 후속 처리는 Bazarr+가 결정한다. SRT, ASS, SSA는 원본 형식을 유지하고 SMI는 SAMI로 반환한다. 자동 번역, 싱크와 형식 변환은 이 플러그인의 기능이 아니다.

## 실패 확인

- 설치 실패: `Activity`에서 누락 파일, SHA-256 불일치, 의존성 설치, worker import 오류를 확인한다. ZIP을 수정하지 말고 Release에서 다시 받는다.
- 설치했는데 목록에 없음: 재시작 여부와 `My Providers` 활성화를 확인한다.
- 제목 또는 시즌이 모호함: Bazarr+가 넘긴 series, season, episode와 Anissia 작품 번호를 비교하고 매핑한다.
- 후보가 있는데 자동 다운로드되지 않음: 언어 프로필, HI 및 forced 조건, 최소 점수와 provider 우선순위를 확인한다.
- 제한 시간 초과: `timeout`은 최대 20초다. 추가 블로그 수를 줄이거나 CLI의 별도 검색 보고서로 출처를 점검한다. worker의 호스트 제한 시간이 더 짧으면 호스트가 먼저 중단할 수 있다.
- 일부 출처 실패: 유효 후보가 있으면 반환하되 stderr에 경고를 남긴다. 필요한 탐색이 실패하고 유효 후보도 없으면 빈 성공 목록으로 숨기지 않고 오류를 반환한다.
- 다운로드 실패: 게시물 삭제, 첨부 변경, 모호한 ZIP, 잘못된 인코딩, 비한국어 자막, TLS 오류와 URL 정책 위반을 확인한다. TLS 인증서 검증을 끄지 않는다.

## 플랫폼과 독립 CLI의 차이

플러그인 ZIP은 OS별 바이너리를 담지 않는다. Bazarr+ host의 지원 OS와 Python worker가 실행 조건을 결정한다. Python 3.15는 이번 지원 범위가 아니다.

CLI는 별도 배포물이다. `linux-x64`, `linux-arm64`, `darwin-x64`, `darwin-arm64`, `windows-x64`, `windows-arm64`의 런타임 포함 압축 파일을 사용한다. Linux와 macOS는 tar.gz, Windows는 ZIP이다. 전체 폴더를 풀어 실행하며 `_internal`을 삭제하거나 실행 파일만 옮기지 않는다.

Linux CLI는 glibc와 시스템 CA가 필요하다. Alpine musl용이 아니다. OpenSSL의 libssl과 libcrypto는 번들에 포함한다. 빌드 Python이 두 모듈을 libpython에 정적으로 포함했다면 별도 공유 라이브러리 대신 그 런타임을 포함한다. macOS CLI는 빌드 Python의 CA 묶음을 포함하고 `SSL_CERT_FILE`을 지정했다면 그 값을 유지한다. Windows CLI는 Python의 기본 SSL context를 통해 Windows CA 저장소를 읽는다. 인증서 검증은 유지된다. OS 최소 버전과 실제 6개 네이티브 실행 결과는 CI 검증 없이 보장하지 않는다.

## 참조

CI는 catalog SDK `57b93e69c760feab1ed7ae0126e4a84916cbf57e`와 Bazarr+ worker `76528c2fb9173442c827405b351f6baaecb37578`의 계약을 검사한다. SDK가 공식 저장소의 출처 메타데이터만 허용하므로 임시 사본에서 해당 메타데이터만 정규화한다. 배포 ZIP의 실제 출처와 해시는 바꾸지 않는다.

- [Bazarr+ Provider Hub](https://lavx.github.io/bazarr/guides/provider-hub.html)
- [Provider Hub SDK](https://github.com/LavX/bazarr-provider-catalog/tree/main/sdk)
- [Python 독립 배포 설계](superpowers/specs/2026-11-11-python-standalone-design.md)
