from setuptools import setup

setup(name='birdtradebot',
      version='0.2.0',
      description='trade crypto on GDAX based on tweets',
      url='https://github.com/nunoloureiro/birdtradebot',
      download_url = 'https://github.com/nunoloureiro/birdtradebot/tarball/0.2.0',
      author='Joao Poupino / Nuno Loureiro',
      author_email='joao@probely.com, nuno@probely.com',
      license='MIT',
      packages=['birdtradebot'],
      package_data={'birdtradebot': ['*.py', './rules/*']},
      zip_safe=True,
      install_requires=[
      		'twython', 'gdax', 'pycrypto', 'python-dateutil'
      	],
      entry_points={
        'console_scripts': [
            'birdtradebot=birdtradebot:go',
        ],},
      keywords=['bitcoin', 'btc', 'ethereum', 'eth', 'twitter'],
      classifiers=[
          'Programming Language :: Python :: 2',
          'Programming Language :: Python :: 3',
          'Intended Audience :: Developers',
          'Intended Audience :: Science/Research',
          'Operating System :: MacOS',
          'Operating System :: Unix',
          'Topic :: Utilities'
        ]
    )
