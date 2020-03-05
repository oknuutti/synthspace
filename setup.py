
from setuptools import setup, find_packages


# with open('../README.md') as f:
# 	long_description = f.read()
# 	long_description = re.sub(r'</?div[^>]*>|\r', '', long_description, flags=re.M)


setup(
    name='synthspace',
    version='0.1',
    packages=find_packages(include=['synthspace*']),

    # Declare your packages' dependencies here, for eg:
    install_requires=['visnav @ git+https://github.com/oknuutti/visnav-py'],

    author='Olli Knuuttila',
    author_email='olli.knuuttila@gmail.com',

    summary='Synthetic Images for Scenes Near Solar-System Small Bodies',
    url='https://github.com/oknuutti/synthspace',
    license='MIT',
)