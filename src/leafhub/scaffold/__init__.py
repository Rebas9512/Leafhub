"""
Scaffold generators for LeafHub consumer projects.

Reads a parsed leafhub.toml manifest (as dict) and generates standardized
setup.sh / install.sh scripts with project-specific customizations.
"""

from .generators import generate_setup_sh, generate_install_sh

__all__ = ["generate_setup_sh", "generate_install_sh"]
