@echo off
REM Build Java Agent (Windows, JDK 8+)
REM Keep this file pure ASCII: cmd reads it as GBK, and UTF-8 comments get
REM mangled into stray characters that split the command lines apart.
cd /d %~dp0
if exist out rmdir /s /q out
mkdir out
javac -encoding UTF-8 -d out src\com\pekaflow\agent\*.java
if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)
jar cfe deploy-agent.jar com.pekaflow.agent.AgentMain -C out .
if errorlevel 1 (
  echo PACKAGING FAILED
  exit /b 1
)
REM Record which sources this jar was built from. The jar is a build artifact
REM committed to the repo, so editing .java without rebuilding leaves the
REM platform serving a stale jar - and the only symptom is "the new feature
REM does nothing", which nobody traces back to a missed compile.
python "%~dp0src_fingerprint.py" --write
if errorlevel 1 (
  echo FINGERPRINT FAILED
  exit /b 1
)
echo.
echo OK: deploy-agent.jar
echo Example:
echo   java -jar deploy-agent.jar --server http://localhost:8080 --name my-agent --tags linux,maven --poll 2 --concurrency 8 --enroll-token ^<token^>
