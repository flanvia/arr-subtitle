# arr-subtitle

[![CI](https://github.com/flanvia/arr-subtitle/actions/workflows/build.yml/badge.svg)](https://github.com/flanvia/arr-subtitle/actions/workflows/build.yml)

Anissia와 자막 제작자 블로그에서 한국어 애니메이션 자막을 찾는 C# CLI다. SQLite나 영속 인덱스 없이 실행하며 NativeAOT 바이너리로 배포한다.

## 기능

- 영상 파일 및 폴더에서 작품, 시즌과 회차 식별
- 작품과 회차 범위를 지정해 영상 없이 자막 수집
- AniList의 원제로 영문 및 로마자 제목을 Anissia 작품과 연결
- Anissia 해당 회차, 제작자 블로그, 사용자 등록 블로그 순서의 fallback
- 네이버, Blogspot, 티스토리 URL 판별과 일반 피드 및 HTML 탐색
- SMI, SRT, ASS, SSA 및 ZIP 검증과 기존 자막 보호
- JSON 결과와 실행별 manifest, 누락과 탐색 실패 구분

독립적인 C# 프로젝트이며 다른 저장소의 fork나 Python 실행 wrapper가 아니다.

## 배포 대상

| OS | x64 | ARM64 (aarch64) |
| --- | --- | --- |
| Linux | `linux-x64` | `linux-arm64` |
| macOS (Darwin) | `osx-x64` | `osx-arm64` |
| Windows | `win-x64` | `win-arm64` |

[Releases](https://github.com/flanvia/arr-subtitle/releases)에서 OS와 CPU에 맞는 압축 파일을 받는다. macOS 파일명에는 `darwin`, Windows에는 `windows`를 사용한다. .NET의 RID에서는 각각 `osx`, `win`이다. `SHA256SUMS`로 파일 무결성을 확인할 수 있다.

.NET 런타임을 별도로 설치할 필요는 없다. Linux 바이너리는 glibc 기반이며 Alpine의 musl은 대상이 아니다. TLS 및 국제화 관련 OS 라이브러리는 필요할 수 있다. macOS와 Windows 바이너리는 개발자 인증서로 서명하지 않는다. 각 대상의 실제 테스트 및 NativeAOT smoke 결과는 [Actions](https://github.com/flanvia/arr-subtitle/actions)에서 확인한다.

## 실행

Linux 및 macOS:

```sh
./arr-subtitle collect --anime-id 3433 --episodes 1-5 --output ./subtitles --json
./arr-subtitle search '흉란영애'
./arr-subtitle match '/media/Kyouran Reijou Nia Liston/Season 1/Kyouran Reijou Nia Liston - 05.mkv'
./arr-subtitle blogs add https://example.blogspot.com/
```

Windows PowerShell:

```powershell
.\arr-subtitle.exe collect --anime-id 3433 --episodes 1-5 --output .\subtitles --json
.\arr-subtitle.exe match 'D:\Anime\Kyouran Reijou Nia Liston\Season 1\Kyouran Reijou Nia Liston - 05.mkv'
```

기본 경로 구조는 `작품/Season <num>/영상`이다. `Working!!`, `Working'!!`, `Working!!!`처럼 시즌별 제목이 다르면 제목을 보존해 식별한다. 근거가 부족하면 임의로 시즌을 추측하지 않고 명시 매핑을 요구한다.

전체 명령과 설정은 [CLI 사용법](docs/cli.md)을 참고한다.

`search`는 검색어와 관련된 작품들의 자막을 과거 회차까지 나열한다. 최신 링크에서 멈추지 않으며 제작자와 등록 블로그를 모두 조사한다. 중간 회차 누락이나 조사 실패는 결과에 표시하고 실패 종료한다. 자세한 완전성 기준은 [전체 검색](docs/search.md)에 있다.

## 개발

`global.json`의 .NET 10 SDK를 사용한다. NativeAOT 게시에는 해당 OS의 C/C++ 도구가 필요하다.

```sh
dotnet restore ArrSubtitle.slnx --locked-mode
dotnet build ArrSubtitle.slnx -c Release --no-restore
dotnet test tests-dotnet/ArrSubtitle.Tests -c Release --no-build
dotnet publish src/ArrSubtitle.Cli -c Release -r linux-x64 -o artifacts/linux-x64
```

테스트와 smoke 검증은 외부 자막 사이트를 호출하지 않는다. [CI/CD](docs/releasing.md)는 6개 대상에서 테스트, NativeAOT 게시와 실제 바이너리 실행을 수행한다.

## 제한

모든 블로그나 삭제된 자막을 찾는다고 보장하지 않는다. 허용한 출처에서도 찾지 못한 회차는 실패로 반환한다. 로그인과 CAPTCHA 우회, JavaScript 실행, 영상 다운로드, 자동 싱크 조정과 자막 변환은 하지 않는다.

Sonarr Custom Script와 Bazarr provider 연동은 후속 범위다. 현재는 CLI와 공통 라이브러리를 제공한다.
