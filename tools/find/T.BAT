@echo off
echo === 1 basic === >> RESULT.TXT
FIND "beta" TEST.TXT >> RESULT.TXT
if errorlevel 1 echo RC=1-NOTFOUND >> RESULT.TXT
if not errorlevel 1 echo RC=0-FOUND >> RESULT.TXT
echo === 2 ignore case === >> RESULT.TXT
FIND /I "beta" TEST.TXT >> RESULT.TXT
echo === 3 line numbers === >> RESULT.TXT
FIND /N /I "beta" TEST.TXT >> RESULT.TXT
echo === 4 invert === >> RESULT.TXT
FIND /V /I "beta" TEST.TXT >> RESULT.TXT
echo === 5 count === >> RESULT.TXT
FIND /C /I "beta" TEST.TXT >> RESULT.TXT
echo === 6 absent === >> RESULT.TXT
FIND "zzzznotpresent" TEST.TXT >> RESULT.TXT
if errorlevel 1 echo RC=1-NOTFOUND >> RESULT.TXT
if not errorlevel 1 echo RC=0-WRONG >> RESULT.TXT
echo === 7 big file across blocks === >> RESULT.TXT
FIND /N "NEEDLE" BIG.LOG >> RESULT.TXT
echo === 8 real use case === >> RESULT.TXT
FIND "avg_fps" BIG.LOG >> RESULT.TXT
echo === 9 stdin === >> RESULT.TXT
TYPE TEST.TXT | FIND /I "gamma" >> RESULT.TXT
echo === 10 missing file === >> RESULT.TXT
FIND "x" NOSUCH.TXT >> RESULT.TXT
if errorlevel 2 echo RC=2-ERROR >> RESULT.TXT
echo === DONE === >> RESULT.TXT
