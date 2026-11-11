# 독립 저장소와 CI/CD

## Python v0.2 배포

현재 배포 workflow는 `.github/workflows/python.yml`이다. Python 3.12~3.14 테스트와 Bazarr+ 독립 패키지 검증, 6개 OS 및 CPU의 PyInstaller 빌드가 모두 통과해야 태그 Release를 공개한다. 플러그인 ZIP 하나와 CLI 압축 파일 6개, `SHA256SUMS`를 제공한다.

CLI는 onedir 형태로 Python과 의존성을 포함한다. Linux 및 macOS는 tar.gz, Windows는 ZIP이다. 플랫폼별 실제 실행과 TLS 정상 및 잘못된 인증서 거부를 확인한다. Linux의 별도 컨테이너 검사에서는 호스트 Python과 .NET을 사용하지 않는다.

플러그인은 `scripts/build_plugin.py`가 Core와 어댑터를 복사하고 manifest의 파일 및 bundle SHA-256을 생성한다. 고정 upstream SDK 및 worker와 격리 실행을 검사한다. 로컬 ZIP 설치에는 CLI가 필요 없다. 커뮤니티 카탈로그의 원격 설치는 manifest가 가리키는 commit과 경로에 실제 Python 파일이 게시되어야 하므로 Release ZIP 공개만으로 충족된 것으로 처리하지 않는다.

기존 C# workflow `build.yml`은 수동 검사만 가능하며 Release 공개는 비활성화했다. Python의 전체 검사 조건을 우회해 C# 자산으로 v0.2 Release가 먼저 공개되는 것을 막는다. 아래 내용은 v0.1 배포의 기록이다.

## 범위

저장소는 `flanvia/arr-subtitle`이며 public이다. 원본 Python 저장소의 Git 이력, Python 코드, 가상 환경과 다운로드 데이터는 포함하지 않는다. 기존 작업 폴더도 변경하지 않는다.

배포 방식은 GitHub Actions 검사와 태그 기반 GitHub Releases다. 컨테이너 배포는 포함하지 않는다. Linux, macOS(Darwin), Windows 각각 x64와 ARM64에서 네이티브 게시 및 실행을 검증한다. 인증서 서명과 notarization은 초기 범위에서 제외한다.

## 대상과 러너

| RID | 러너 | 파일명 구분 |
| --- | --- | --- |
| `linux-x64` | `ubuntu-24.04` | `linux-x64` |
| `linux-arm64` | `ubuntu-24.04-arm` | `linux-arm64` |
| `osx-x64` | `macos-15-intel` | `darwin-x64` |
| `osx-arm64` | `macos-15` | `darwin-arm64` |
| `win-x64` | `windows-2025` | `windows-x64` |
| `win-arm64` | `windows-11-arm` | `windows-arm64` |

## 검사

push, PR 및 수동 실행에서 다음을 확인한다.

1. 고정 SDK와 NuGet lock 파일로 의존성 복원
2. Release 빌드 및 전체 테스트
3. 대상 RID의 CLI와 fixture 실행기를 NativeAOT로 게시
4. 게시된 CLI의 도움말 및 버전 실행
5. 게시된 fixture 실행기의 직접 다운로드, 12화 합본과 미발견 시나리오 실행
6. 실행 파일과 설정 예제를 압축하고 artifact 업로드

Linux는 `scripts/build-openssl.sh`에서 고정 OpenSSL 3.5.9 소스의 SHA-256을 검증한 뒤 정적 라이브러리를 빌드한다. CLI와 smoke에 `StaticOpenSslLinking=true`를 적용한다. OpenSSL 4 기반 Fedora 컨테이너에서도 HTTPS와 인증서 검증을 실행한다. 배포물에 `OPENSSL-LICENSE.txt`와 `OPENSSL-VERSION`을 포함한다. 보안 수정은 OpenSSL 버전과 해시를 갱신한 새 빌드를 배포해야 하며 사용자 시스템 OpenSSL 업데이트만으로 포함된 버전이 갱신되지는 않는다.

CI에서 외부 자막 파일을 수집하지 않는다. 비밀 값 없는 fixture로 네트워크와 파일 검증을 재현한다. 외부 사이트의 일시 장애로 기본 CI가 실패하지 않게 한다.

검사 작업은 저장소 읽기 권한만 사용한다. action은 commit SHA로 고정한다. PR의 코드를 저장소 쓰기 권한으로 실행하지 않는다.

## Release

`v*` 태그 push에서 동일한 6개 대상 검사가 모두 성공한 뒤 Release 작업을 수행한다. Release 권한은 해당 작업에만 부여한다.

- Linux 및 macOS: `.tar.gz`
- Windows: `.zip`
- 전체 파일의 `SHA256SUMS`

Release 작업은 태그가 현재 기본 브랜치 이력에 포함된 commit을 가리키는지 확인한다. 일부 대상이 실패하면 불완전한 새 Release를 공개하지 않는다. 태그 이름을 shell 명령에 직접 삽입하지 않는다.

## 구현 진행

- [x] 기존 C# 소스와 테스트만 별도 폴더에 복사
- [x] 6개 대상 의존성과 OS 경로 및 파일명 호환성 확인
- [x] CI 및 태그 Release workflow 작성
- [x] 공개할 파일과 작성자 확인 후 독립 Git 이력 생성
- [x] public 저장소 생성 및 push
- [x] GitHub Actions 결과 확인 및 실패 수정
- [ ] 초기 태그 Release와 6개 배포물 확인

첫 CI 실행에서 6개 대상 모두 테스트, NativeAOT 게시 및 바이너리 smoke를 통과했다. 실행 기록: https://github.com/flanvia/arr-subtitle/actions/runs/42742403372
