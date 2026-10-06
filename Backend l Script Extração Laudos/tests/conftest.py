"""Deixa os módulos da pipeline importáveis pelos testes (eles ficam na pasta de cima)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
