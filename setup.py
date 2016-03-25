from setuptools import setup

setup(
    name='lektor-i18n',
    version='0.1',
    author=u'NumeriCube',
    author_email='support@numericube.com',
    license='GPL',
    py_modules=['lektor_i18n'],
    entry_points={
        'lektor.plugins': [
            'i18n = lektor_i18n:I18NPlugin',
        ]
    }
)
