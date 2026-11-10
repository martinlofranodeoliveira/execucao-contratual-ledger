# Dependências e avisos reais

Runtime Python do projeto: biblioteca padrão e este pacote; nenhuma distribuição
PyPI adicional. Imagem fixada por digest: CPython3.13.18 com SQLite3.40.1 observado.
Python/SQLite e bibliotecas do sistema pertencem aos respectivos mantenedores.

Build usa cinco wheels oficiais com versões/hashes em requirements-build.lock:
build1.6.1, packaging26.3, pyproject_hooks1.3.3, setuptools84.2.1 e wheel0.48.0.
O bootstrap pip26.2.2 pertence à imagem Python fixada. Não são dependências runtime
do SDK. O ambiente privado inicial de qualificação era sobrecompleto; a imagem
pública de check é reconstruída do Python fixado somente com estes cinco wheels.

notices/build-manifest.json identifica23 textos extraídos byte-a-byte desses cinco
wheels. notices/base-manifest.json identifica140 arquivos efetivos de copyright do
sistema, licença Python e licenças do wheel ensurepip da imagem fixada. Arquivos
neutros/planos preservam os bytes originais, caminho upstream e SHA256; o verificador
recusa ausência, alteração e symlinks. Essa é uma inventariação técnica dos textos
realmente distribuídos, não uma certificação jurídica ou relicenciamento upstream.
O MIT em LICENSE aplica-se ao código original deste projeto.
