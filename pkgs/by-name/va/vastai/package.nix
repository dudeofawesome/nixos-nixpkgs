{ lib, python3Packages, fetchPypi }:
python3Packages.buildPythonApplication rec {
  pname = "vastai";
  version = "0.2.2";

  src = fetchPypi {
    inherit pname version;
    sha256 = "sha256-pQbCVxdt6KAXdO8Y/QooB1Zo1Rc/s2Kmfnd56rI5m8U=";
  };

  disabled = !python3Packages.isPy3k;

  # upstream has no code tests
  doCheck = false;

  meta = with lib; {
    mainProgram = "vast.py";
    homepage = "https://github.com/vast-ai/vast-python";
    maintainers = [ maintainers.dudeofawesome ];
    description = "The Vast.ai CLI";
    license = licenses.mit;
    platforms = platforms.all;
  };
}
