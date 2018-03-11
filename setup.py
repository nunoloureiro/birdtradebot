from setuptools import setup

setup(name='birdtradebot',
      version='0.3.0',
      description='trade crypto-currencies based on tweets',
      url='https://github.com/nunoloureiro/birdtradebot',
      download_url = 'https://github.com/nunoloureiro/birdtradebot/tarball/0.3.0',
      author='Joao Poupino / Nuno Loureiro',
      author_email='joao@probely.com, nuno@probely.com',
      license='MIT',
      packages=['birdtradebot'],
      package_data={'birdtradebot': ['*.py', './config/*']},
      zip_safe=True,
      install_requires=[
          'twython', 'gdax', 'ccxt, ''pycryptodome', 'python-dateutil', 'ccxt'
      ],
      entry_points={
        'console_scripts': [
            'birdtradebot=birdtradebot:go',
        ],
      },
      keywords=['bitcoin', 'btc', 'ethereum', 'eth', 'twitter'],
      classifiers=[
          'Programming Language :: Python :: 3',
          'Intended Audience :: Developers',
          'Intended Audience :: Science/Research',
          'Operating System :: MacOS',
          'Operating System :: Unix',
          'Topic :: Utilities'
        ]
    )
