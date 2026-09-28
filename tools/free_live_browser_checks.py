"""Original browser acceptance plus capture-freshness checks on every page."""
import argparse
from unittest.mock import patch
from tools.check_free_pages import check


def run(root, output):
    from selenium.webdriver.remote.webdriver import WebDriver
    original = WebDriver.execute_script
    checked = set()
    def execute(driver, script, *args):
        result = original(driver, script, *args)
        if result == 'ready' and 'dataset.staticReady' in script:
            info = original(driver, "const n=document.querySelector('.price-freshness');return n?{text:n.textContent,captured:n.dataset.capturedAt}:null;")
            if not info or not info['text'] or 'snapshot:' not in info['text'] and 'captured:' not in info['text']:
                raise AssertionError('Missing capture-derived freshness indicator')
            checked.add(driver.current_url)
        return result
    with patch.object(WebDriver, 'execute_script', execute):
        report = check(root, output)
    if len(checked) < 9:
        raise AssertionError('Not enough freshness-aware navigation checks')
    print('FREE_FRESHNESS_BROWSER checked_pages='+str(len(checked)), flush=True)
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--output',default='free-browser-results')
    args=parser.parse_args()
    run(args.directory,args.output)
