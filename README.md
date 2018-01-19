# birdtradebot v0.2.0

This bot is originally based on vickitrix bot (https://github.com/nellore/vickitrix), but heavily modified to become more robust and to support new rules.

`birdtradebot` trades on GDAX according to rules based on tweets. Trading involves 2 main components: 1) defines the strategy and provides advice to open or close a position; 2) places the orders and manages the wallets. This bot provides the latter, it listens on twitter for advice and places orders based on that advice. Placing orders and managing wallets is actually more complex than what it sounds and we believe there's still a lot of potential for improvement.

## Preliminaries

Though `birdtradebot` should work on Python 2 or 3 in Windows/UNIX, it's been tested mostly on Python 3 on a Macbook.

You'll need some keys and secrets and passcodes from Twitter and GDAX. `birdtradebot` will store these in a config file (`~/.birdtradebot/config`) on disk, but it ensures the secrets and passcodes are AES256-encrypted so evildoers who grab your laptop while you're logged in can't easily swipe working credentials.

1. Open a new browser tab, and use it to [create a new Twitter app](https://apps.twitter.com/) after logging into Twitter. Name and describe it however you like, and specify no callback URL, but note Twitter also requires you enter some well-formed website URL to finish the process. You're allowed to write something like `https://placeholder.com/placeholder`. The thing will complain if your description isn't long enough, too. So dumb.
2. Click the `Keys and Access Tokens` tab.
3. Click `Create my access token`. The tab should now display a consumer key, a consumer secret, an access token, and an access token secret. Leave this tab be for now.
4. Open a new browser tab, and use it to [visit GDAX](https://gdax.com). Log in, click your avatar on the upper right, and click `API`.
5. Create an API key with permissions to view and trade. You should now see a key, secret, and passphrase. Don't close the tab, or you'll lose the secret forever---which isn't the end of the world; you'll just have to regenerate an API key.

## Install and configure `birdtradebot`
1. Clone the repo, `cd` into it, `pip install` the required packages `python-dateutil`,  [`twython`](https://github.com/ryanmcgrath/twython), [`gdax`](https://github.com/danpaquin/GDAX-Python), and [`pycrypto`](https://pypi.python.org/pypi/pycrypto), and precede all `birdtradebot` commands below with `python3`. Your choice.
2. Configure `birdtradebot` by running

        birdtradebot configure
    This allows you to create (or overwrite) a profile with a name of your choice. (Entering nothing makes the profile name `default`, which is nice because then you won't have to specify the profile name at the command line when you `birdtradebot trade`.) You'll be asked to enter credentials from the browser tabs you left open in Preliminaries. You'll also be asked to enter a password, which you'll need every time you `birdtradebot trade`.
3. Grab and edit the rules in [`rules/birdpersonborg.py`](rules/birdpersonborg.py) so they do what you want. `rules/birdpersonborg.py` creates a Python list of dictionaries called `rules`, where each dictionary has the following keys:
    * `handles`: a list of the Twitter handles to which the rule should apply, where commas are interpreted as logical ORs. At least one of `handles` or `keywords` must be specified in a rule. However, nothing is stopping you from passing an empty list, which `birdtradebot` interprets as no filter---but do this at your own peril.
    * `keywords`: a list of keywords from tweets to which the rule should apply, where commas are interpreted as logical ORs. If both `handles` and `keyword` are specified, there's a logical OR between the two lists as well.
    * `orders`: a list of orders. Each item is a dictionary of HTTP request parameters for an order as described in the [GDAX docs](https://docs.gdax.com/#orders). `birdtradebot` respects default values of parameters given there if any are left out in a given rule. Some details on particular keys from the `order` dictionary:
        * `product_id`: a valid [GDAX product ID](https://docs.gdax.com/#products). It looks like `<base currency>-<quote currency>`.
        * `funds`, `size`, `price`: the value may be any Python-parsable math expression involving any of the following: (1) `{tweet}`: the content of the current matched tweet; (2) `{available[<currency>]}`: here, `<currency>` is one of `ETH`, `BTC`, `LTC`, and `USD`. `birdtradebot` sets `{available[<currency>]}` to the amount of `<currency>` available for trading in your account right before making an order. You can use regular expressions with Python's [`re`](https://docs.python.org/2/library/re.html) module; (3) `{inside_bid}`: the most recent inside (i.e., best) bid from the [product order book](https://docs.gdax.com/#get-product-order-book) for the order's `product_id` at the time the order is placed; (4) `{inside_ask}`: the most recent inside (i.e., best) ask from the product order book for the order's `product_id` at the time the order is placed.
    * `condition`: any Python-parsable expression involving `{tweet}` and `{available[<currency>]`. Regular expressions can be used here with the `re` module.
With the default rules, you split your available fiat funds to buy ETH and BTC when @birdpersonborg goes long, and you sell all the ETH and BTC you can when @birdpersonborg goes short.
4. Run
        
        birdtradebot trade --profile <profile name> --rules <rules file>
        
   , and enter the profile's password. Leave out the `--profile` to use the default profile, and leave out `--rules` to use the default `rules/birdpersonborg.py`. `birdtradebot` will listen for tweets that match the conditions from the rules in `birdpersonborg.py` and perform the specified actions.
   If after a trade's been made, the "available to trade" line makes it look like currency vanished into thin air, don't fret; this probably means the trade hasn't completed yet. You can increase the sleep time after a trade is requested and before the "available to trade" line is displayed with `--sleep`.

## Contributing

Pull requests are welcome! Fork freely! If you've written a substantial contribution, and you'd like to be added as a collaborator, reach out to me.

## Disclaimer

If you use this software, you're making and/or losing money because someone or something you probably don't know tweeted, which is totally crazy. Don't take risks with money you can't afford to lose.

Also note this part of the MIT license:
```
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
