{
  description = "Lazarr development environment with native libtorrent and ffmpeg";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = nixpkgs.lib.genAttrs systems;
    in {
      packages = forAll (system: {
        flaresolverr = nixpkgs.legacyPackages.${system}.flaresolverr;
      });
      devShells = forAll (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (p: with p; [
            fastapi uvicorn sqlalchemy alembic httpx jinja2 pydantic python-multipart
            cryptography argon2-cffi beautifulsoup4 packaging pytest pytest-asyncio
            libtorrent-rasterbar setuptools websockets pillow langdetect
          ]);
        in {
          default = pkgs.mkShell {
            LAZARR_TEST = "1";
            packages = [ python pkgs.ffmpeg-headless pkgs.ruff pkgs.nodejs ];
            shellHook = ''
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              echo "Lazarr: python -m lazarr.cli serve (first account is created in the browser)"
            '';
          };
        });
    };
}
