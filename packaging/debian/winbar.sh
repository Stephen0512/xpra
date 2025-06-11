#!/bin/bash

set -e

if [ -z "${REPO_ARCH_PATH}" ]; then
	REPO_ARCH_PATH="`pwd`/../repo"
fi

commit="d69731f7482e5604cc7592e1241e12c69367e2cb"
ZIP=`ls ../pkgs/${commit}.zip`
dirname="winbar-${commit}"
rm -fr "${dirname}"
unzip -q ${ZIP}
pushd "./${dirname}"
ln -sf ../winbar ./debian

#install build dependencies:
mk-build-deps --install --tool='apt-get -o Debug::pkgProblemResolver=yes --yes' debian/control
rm -f winbar-build-deps*

debuild -us -uc -b

ls -la ../winbar*deb
mv ../winbar*deb ../winbar*changes "$REPO_ARCH_PATH"
popd
