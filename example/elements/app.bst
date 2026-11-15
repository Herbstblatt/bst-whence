# SPDX-License-Identifier: MIT
kind: compose

# In BuildStream 2, compose elements take build-dependencies: those are the
# elements whose artifacts get composed together into this one.
build-depends:
- base.bst
- lib.bst
