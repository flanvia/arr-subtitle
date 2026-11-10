# C# CLI

## 빌드 및 실행

.NET SDK는 `global.json`의 버전을 사용한다. NativeAOT 게시에는 C 컴파일러 및 링크 도구와 플랫폼 개발 라이브러리가 필요하다.

```sh
dotnet build ArrSubtitle.slnx -c Release
dotnet test tests-dotnet/ArrSubtitle.Tests -c Release
dotnet publish src/ArrSubtitle.Cli -c Release -r linux-x64 -o artifacts/linux-x64
```

Linux, macOS 및 Windows의 x64와 ARM64를 지원 대상으로 한다. .NET RID는 `linux-x64`, `linux-arm64`, `osx-x64`, `osx-arm64`, `win-x64`, `win-arm64`다. 각 OS와 CPU의 러너에서 NativeAOT 게시와 실행을 검증한다.

Linux에는 clang 및 개발 라이브러리, macOS에는 Xcode Command Line Tools, Windows에는 해당 아키텍처용 Visual C++ 도구가 필요하다. 게시된 바이너리는 .NET 런타임을 포함하지만 완전 정적 바이너리는 아니다. macOS 및 Windows 배포물은 개발자 인증서로 서명하지 않는다.

## 영상 기반

전체 자막 목록 검색은 다음과 같이 실행한다. 결과를 다운로드하지 않으며 과거 게시물과 모든 등록 블로그를 조사한다. 일부 출처를 조사하지 못했거나 중간 회차가 비면 종료 코드 1이다.

```sh
arr-subtitle search '흉란영애'
arr-subtitle search '흉란영애' --json
```

```sh
artifacts/linux-x64/arr-subtitle match '/media/Kyouran Reijou Nia Liston/Season 1/Kyouran Reijou Nia Liston - 05.mkv'
artifacts/linux-x64/arr-subtitle download '/media/Kyouran Reijou Nia Liston/Season 1/Kyouran Reijou Nia Liston - 05.mkv'
artifacts/linux-x64/arr-subtitle scan '/media/anime'
```

`match`는 게시물과 첨부 후보를 찾는다. 다운로드 내용 검증과 실제 영상의 싱크를 보장하지 않는다. `download`는 자막 내용을 검증한 뒤 영상 옆에 저장한다. 원본 영상은 변경하지 않는다. 검색 및 다운로드는 파일명 경로로 동작할 수 있으므로 가짜 영상을 만들 필요가 없다.

기본 경로는 `작품/Season 2/영상.mkv`다. 영문과 로마자는 AniList 원제와 Anissia를 연결한다. 작품이나 시즌이 모호하면 작품 번호 또는 설정 매핑을 요구한다. `Working!!`, `Working'!!`, `Working!!!`를 같은 문자열로 합치지 않는다. 라이브러리 시즌과 작품 DB의 구분이 항상 같지 않으므로 근거 없는 시즌 대응은 자동 선택하지 않는다.

## 영상 없이 수집

```sh
artifacts/linux-x64/arr-subtitle collect --anime-id 3433 --episodes 1-5 --output ./subtitles
artifacts/linux-x64/arr-subtitle collect 'Kyouran Reijou Nia Liston' --season 1 --episodes 1,2,3,4,5 --output ./subtitles
```

요청 회차를 명시해야 한다. 블로그에서 발견한 마지막 회차를 전체 화수로 간주하지 않는다. 소수 회차는 `12.5`로 지정한다. 요청 범위는 최대 1000개다.

## 전역 fallback 블로그

기본 설정은 사용자 설정 디렉터리의 `arr-subtitle/config.json`이다. 다른 설정은 `--config`로 지정한다. 명시한 파일이 없으면 오류다. 다음 예시는 기본 설정 파일을 생성하거나 수정한다.

```sh
artifacts/linux-x64/arr-subtitle blogs add https://bluewater91.blogspot.com/
artifacts/linux-x64/arr-subtitle blogs list
artifacts/linux-x64/arr-subtitle blogs remove https://bluewater91.blogspot.com/
```

블로그 홈과 개별 게시물 주소를 받는다. 알려진 플랫폼은 URL에서 식별하고 사용자 지정 도메인은 문서 구조를 확인한다. 등록 순서대로 fallback한다. 같은 네이버 blogId의 PC와 모바일 주소는 중복 등록하지 않는다. 알려지지 않은 사이트는 일반 HTTP 탐색을 사용하며 모든 사이트의 동작을 보장하지 않는다.

탐색 순서는 Anissia의 해당 회차, 제작자 블로그, 등록한 전역 블로그다. 공개 첨부 저장소로의 다운로드는 허용하지만 미등록 블로그나 웹 검색으로 확장하지 않는다.

## 시즌 매핑

```json
{
  "aniListEnabled": true,
  "blogs": [],
  "mappings": [
    {
      "title": "라이브러리 작품명",
      "season": 2,
      "animeId": 1234,
      "aliases": ["Release Title"],
      "libraryPath": "/media/라이브러리 작품명",
      "episodeOffset": 0
    }
  ]
}
```

위 작품 번호는 형식 설명을 위한 값이다. 실제 번호를 조회해 바꿔야 한다. `libraryPath`가 있으면 해당 작품 경로에만 적용한다. `episodeOffset`은 영상 회차에 더해 Anissia 및 블로그의 회차로 사용한다. 결과 파일명은 영상의 원래 회차를 유지한다. 기존 파일과 설정의 동시 변경 충돌은 덮어쓰지 않고 실패한다.

## 결과

`--json`은 stdout에 JSON 결과만 출력한다. 실행별 manifest에는 성공과 실패 회차, 출처, 오류 및 자막 해시를 기록한다. 영상 기반 manifest는 사용자 데이터 디렉터리의 `arr-subtitle/runs/`, 작품 기반 manifest는 지정 출력 폴더에 저장한다. manifest를 캐시나 성공 색인으로 다시 읽지 않는다.

| 코드 | 의미 |
| --- | --- |
| 0 | 요청한 회차를 모두 처리했거나 검색 후보를 확보함 |
| 1 | 일부 회차 실패 또는 실행 및 저장 실패 |
| 2 | 잘못된 인수나 설정 |
| 130 | 사용자 취소 |

`NotFound`는 허용 출처를 조사했지만 못 찾은 경우다. 접속 실패나 페이지 상한으로 끝내지 못했으면 `SearchIncomplete`다. 일부 성공 파일은 보존한다. 기존 유효한 `.ko` 및 `.kor` sidecar는 덮어쓰지 않는다.

## 현재 제한

- 실제 Jellyfin 인식 및 영상 싱크는 검증하지 않았다.
- Sonarr Custom Script 및 Bazarr provider 어댑터, HTTP 서버와 상시 감시는 아직 없다.
- 로그인, CAPTCHA, JavaScript 실행은 지원하지 않는다.
- RAR, 7z와 중첩 압축은 지원하지 않는다.
- 네이버는 HTTP 목록과 본문 wrapper 기반이다. 플랫폼 구조가 바뀌거나 목록 접근이 제한되면 탐색 미완료로 반환한다.
- 동일 회차의 여러 자막 형식이 들어 있는 ZIP은 자동 우선순위를 추측하지 않고 모호한 결과를 반환할 수 있다.
- 실행 중 응답 재사용에는 128 MiB 상한이 있다. 초과하면 명시적으로 실패한다.
