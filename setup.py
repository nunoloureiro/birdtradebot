from setuptools import setup

setup(name='birdtradebot',
      version='0.4.0',
      description='trade crypto-currencies based on tweets',
      url='https://github.com/nunoloureiro/birdtradebot',
      download_url='https://github.com/nunoloureiro/birdtradebot/tarball/0.4.0',
      author='Joao Poupino / Nuno Loureiro',
      author_email='joao@probely.com, nuno@probely.com',
      license='MIT',
      packages=['birdtradebot'],
      package_data={'birdtradebot': ['*.py', './config/*', './exchanges/*']},
      zip_safe=False,
      install_requires=[
          'twython', 'gdax>=1.0.7', 'ccxt', 'python-dateutil', 'jsonpickle',
          'sortedcontainers'
      ],
      dependency_links=[
          "https://github.com/poupas/gdax-python/tarball/master#egg=gdax-1.0.7"
      ],
      entry_points={
          'console_scripts': [
              'birdtradebot=birdtradebot.run:main',
          ]
      },
      keywords=['bitcoin', 'btc', 'ethereum', 'eth', 'twitter',
                'gdax', 'bitfinex'],
      classifiers=[
          'Programming Language :: Python :: 3',
          'Intended Audience :: Developers',
          'Intended Audience :: Science/Research',
          'Operating System :: MacOS',
          'Operating System :: Unix',
          'Topic :: Utilities'
      ]
)
