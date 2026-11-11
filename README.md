# arr-subtitle

[![Python](https://github.com/flanvia/arr-subtitle/actions/workflows/python.yml/badge.svg)](https://github.com/flanvia/arr-subtitle/actions/workflows/python.yml)

Anissia와 자막 제작자 블로그에서 한국어 애니메이션 자막을 찾는 Python 프로젝트다. 같은 Core를 포함한 Bazarr+ 플러그인과 독립 CLI로 배포한다. SQLite나 별도 검색 서비스는 필요 없다.

## 기능

- 영상 파일 및 폴더에서 작품, 시즌과 회차 식별
- 작품과 회차 범위를 지정해 영상 없이 자막 수집
- AniList의 원제로 영문 및 로마자 제목을 Anissia 작품과 연결
- Anissia 해당 회차, 제작자 블로그, 사용자 등록 블로그 순서의 fallback
- 네이버, Blogspot, 티스토리 URL 판별과 일반 피드 및 HTML 탐색
- SMI, SRT, ASS, SSA 및 ZIP 검증과 기존 자막 보호
- JSON 결과와 실행별 manifest, 누락과 탐색 실패 구분
- 폴더 경로만 입력하는 재귀 탐색과 제한된 병렬 다운로드
- Bazarr+ Provider Hub에서 한국어 회차 자막 검색과 다운로드

Core 소스는 하나지만 배포물은 독립적이다. 플러그인은 CLI를 호출하지 않으며 CLI는 Bazarr+를 요구하지 않는다.

## 배포 대상

| OS | x64 | ARM64 (aarch64) |
| --- | --- | --- |
| Linux | `linux-x64` | `linux-arm64` |
| macOS (Darwin) | `osx-x64` | `osx-arm64` |
| Windows | `win-x64` | `win-arm64` |

[Releases](https://github.com/flanvia/arr-subtitle/releases)에서 OS와 CPU에 맞는 압축 파일을 받는다. macOS 파일명에는 `darwin`, Windows에는 `windows`를 사용한다. .NET의 RID에서는 각각 `osx`, `win`이다. `SHA256SUMS`로 파일 무결성을 확인할 수 있다.

독립 CLI는 Python 런타임과 의존성을 포함한 디렉터리형 배포다. 압축을 풀고 실행하며 시스템 Python이나 .NET을 설치할 필요가 없다. 실행 파일만 따로 옮기지 않는다. Linux는 glibc 기반이며 Alpine musl은 대상이 아니다. TLS 라이브러리를 포함하고 인증서 검증을 유지한다. Linux의 시스템 CA 인증서와 OS 기본 라이브러리는 필요하다. 배포물은 개발자 인증서로 서명하지 않는다. 대상별 빌드와 smoke 결과는 [Actions](https://github.com/flanvia/arr-subtitle/actions/workflows/python.yml)에서 확인한다.

플러그인은 OS 공통 ZIP 하나이며 Core를 포함한다. Python은 Bazarr+ worker의 3.12~3.14 런타임을 사용한다. [Bazarr+ 설치와 설정](docs/bazarr-plus.md)을 참고한다.

## 실행

Linux 및 macOS:

```sh
./arr-subtitle collect --anime-id 3433 --episodes 1-5 --output ./subtitles --json
./arr-subtitle '/media/anime'
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

폴더 경로는 하위 영상을 재귀 탐색한다. 작품과 시즌별로 요청을 묶고 네트워크 요청은 최대 4개를 병렬 실행한다. 같은 ZIP은 공유하고 각 영상 옆에 같은 stem의 `.ko.smi`, `.ko.srt` 등을 저장한다. 기존 유효한 `.ko` 및 `.kor` 자막은 덮어쓰지 않는다. 심볼릭 링크와 junction은 따라가지 않는다.

`search`는 검색어와 관련된 작품들의 자막을 과거 회차까지 나열한다. 최신 링크에서 멈추지 않으며 제작자와 등록 블로그를 모두 조사한다. 중간 회차 누락이나 조사 실패는 결과에 표시하고 실패 종료한다. 자세한 완전성 기준은 [전체 검색](docs/search.md)에 있다.

검색은 기본 60초 제한이며 `search 'Working' --timeout 120`으로 변경할 수 있다. 진행 상황과 발견 회차는 즉시 stderr로 출력한다. 취소하거나 시간이 끝나면 발견한 부분 결과와 미완료 출처를 반환한다.

## 개발

Python 3.12~3.14를 지원한다. 프로젝트 가상환경을 사용한다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -m arr_subtitle --help
.venv/bin/python scripts/build_plugin.py
```

Windows에서는 `.venv\Scripts\python.exe`를 사용하고 `PYTHONPATH`를 `src`로 설정한다. CLI 패키징에는 해시 고정 `requirements-build.txt`를 설치하고 `scripts/build_release.py`를 실행한다. 각 대상 OS와 CPU에서 네이티브로 빌드한다.

테스트와 fixture smoke는 외부 자막 사이트를 호출하지 않는다. [이식 검증](docs/python-parity.md)에서 C# 회귀 조건의 대응과 실제 검색 결과를 확인한다. 기존 v0.1.x C# Release는 보존한다.

## 제한

모든 블로그나 삭제된 자막을 찾는다고 보장하지 않는다. 허용한 출처에서도 찾지 못한 회차는 실패로 반환한다. 로그인과 CAPTCHA 우회, JavaScript 실행, 영상 다운로드, 자동 싱크 조정과 자막 변환은 하지 않는다.

공식 Bazarr용 어댑터, Sonarr Custom Script와 상시 감시는 포함하지 않는다. 플러그인은 Bazarr+ Provider Hub용이다.
