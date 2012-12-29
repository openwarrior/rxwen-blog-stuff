#!/usr/bin/env python2

from OpenSSL import SSL
import sys
import os.path
import subprocess
import re
import netrc
import ConfigParser
import imp
from optparse import OptionParser

from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet import ssl, reactor
from twisted.protocols.policies import TimeoutMixin
from twisted.python import log

class ImapIdleClient(LineOnlyReceiver, TimeoutMixin):
    def __init__(self, username, password, mailbox, account, idle_timeout):
        self.init_state = 'init'
        self.state_machine = { 'init': ('login', self.wait_for_hello)
                             , 'login': ('examine', self.login)
                             , 'examine': ('idle', self.examine)
                             , 'idle': ('done', self.idle)
                             , 'done': ('idle', self.done_idle)
                             }

        self.state = self.init_state
        self.username = username
        self.password = password
        self.mailbox = mailbox
        self.account = account
        self.idle_timeout = idle_timeout

    def connectionMade(self):
        self.factory.resetDelay()

    def connectionLost(self, reason):
        print 'connection for %s:%s lost (%s)' % (self.account, self.mailbox, reason)

    def lineReceived(self, line):
        log.msg('[%s:%s] (%s) %s' % (self.account, self.mailbox, self.state, line))

        (next_state, action) = self.state_machine[self.state]
        if action(line):
            self.state = next_state
            log.msg('[%s:%s] Transitioning to state %s' % (self.account, self.mailbox, self.state))

    def wait_for_hello(self, line):
        if re.match(r'\* OK', line) is not None:
            self.sendLine('. login %s %s' % (self.username, self.password))
            return True
            print 'Logging in'
        else:
            return False

    def login(self, line):
        if re.match(r'\. OK', line) is not None:
            self.sendLine('. examine %s' % self.mailbox)
            return True
            print 'Examining mailbox'
        else:
            return False

    def examine(self, line):
        if re.match(r'\. OK', line) is not None:
            self.sendLine('. idle')
            self.setTimeout(self.idle_timeout)
            return True
            print 'Going idle'
        else:
            return False

    def idle(self, line):
        if re.match(r'\* \d+ (EXISTS|EXPUNGE)', line) is not None:
            subprocess.Popen(['offlineimap', '-a', self.account, '-f', self.mailbox, '-o', '-k', 'mbnames:enabled=no'])
            self.sendLine('DONE')
            return True
            print 'Done idling (new mail)'
        else:
            return False

    def done_idle(self, line):
        if re.match(r'\. OK', line) is not None:
            self.sendLine('. idle')
            self.setTimeout(self.idle_timeout)
            return True
            print 'Idling again'
        else:
            return False

    def timeoutConnection(self):
        self.state = 'done'
        self.sendLine('DONE')
        print 'Done idle (timeout)'

class ImapClientFactory(ReconnectingClientFactory):
    def __init__(self, user, password, mailbox, account, timeout):
        self.username = user
        self.password = password
        self.mailbox = mailbox
        self.account = account
        self.timeout = 60*timeout

    def buildProtocol(self, addr):
        proto = ImapIdleClient(self.username, self.password, self.mailbox, self.account, self.timeout)
        proto.factory = self
        return proto

class AccountOptions(object):
    user = None
    password = None
    server = None
    port = 993
    mailboxes = None
    timeout = 29

def do_eval(cfg, exp):
    if not cfg.has_option('general', 'pythonfile'):
        print 'pythonfile option must be defined to use remotepasseval'
        return None
    pythonfile = cfg.get('general', 'pythonfile')
    pythonfile = os.path.expanduser(pythonfile)
    mod = imp.load_source('pythonfile_mod', pythonfile)
    return eval('mod.'+exp)

def get_repository_detail(repository_name, cfg):
    account = None
    for section in cfg.sections():
        sp = section.split()
        if sp[0].lower() != 'repository' or sp[1] != repository_name:
            continue

        server_type = cfg.get(section, 'type')
        account = AccountOptions()
        if server_type == "imap":
            account.server = cfg.get(section, 'server')
            if cfg.has_option(section, 'port'):
                account.port = cfg.getint(section, 'port')
            boxes = cfg.get(section, 'mailboxes')
            account.mailboxes = re.split('[, \t]+', boxes)
        elif server_type == "Gmail":
            account.server = "imap.gmail.com"
            account.port = 993
            boxes = "INBOX"
            account.mailboxes = re.split('[, \t]+', boxes)

        if cfg.has_option(section, 'remoteuser'):
            account.user = cfg.get(section, 'remoteuser')
        elif cfg.has_option(section, 'remoteusereval'):
            account.password = do_eval(cfg, cfg.get(section, 'remoteusereval'))
        else:
            netconf = netrc.netrc().hosts
            conf = netconf[account.server]
            account.user = conf[0]

        if cfg.has_option(section, 'remotepass'):
            account.password = cfg.get(section, 'remotepass')
        elif cfg.has_option(section, 'remotepasseval'):
            account.password = do_eval(cfg, cfg.get(section, 'remotepasseval'))
        else:
            netconf = netrc.netrc().hosts
            conf = netconf[account.server]
            account.password = conf[2]

    return account

def get_account_detail(account_name, cfg):
    account = None
    for section in cfg.sections():
        if section.split()[0].lower() != 'account':
            continue

        name = section.split(None, 1)[1].strip()
        if name != account_name:
            continue

        repository_name = cfg.get(section, 'remoterepository')
        account = get_repository_detail(repository_name, cfg)
    return account

def parse_options():
    usage = "usage: %s [options] [accounts to sync (overrides config file)]"
    parser = OptionParser(usage=usage)
    parser.set_defaults(config=None)
    parser.add_option('-c', '--configuration', dest='config',
                      action='store', type='string',
                      help='Alternate location of configuration file')
    (options, args) = parser.parse_args()

    if options.config is not None:
        config_path = options.config
    else:
        config_path = os.path.expanduser('~/.offlineimaprc')

    if not os.path.isfile(config_path):
        parser.error("Configuration file %s doesn't exist" % config_path)

    if len(args) > 0:
        accounts = args
    else:
        accounts = None

    cfg = ConfigParser.ConfigParser()
    cfg.read(config_path)

    if cfg.has_section('general'):
        if cfg.has_option('general', 'accounts') and accounts is None:
            accounts = re.split('[, \t]+', cfg.get('general', 'accounts'))

    account_settings = {}

    for name in accounts:
        account = get_account_detail(name, cfg)
        account_settings[name] = account

    if accounts is not None:
        for account in accounts:
            if account not in account_settings:
                parser.error('Account not found: %s' % account)

    if len(account_settings.keys()) == 0:
        parser.error('No accounts defined')

    return account_settings

def main():
    accounts = parse_options()
    #netconf = netrc.netrc().hosts

    log.startLogging(sys.stdout)

    for (name, settings) in accounts.items():
        for mailbox in settings.mailboxes:
            #if settings.server not in netconf:
                #sys.exit('Server "%s" needs an entry in your netrc file' % settings.server)
            factory = ImapClientFactory(settings.user, settings.password, mailbox, name, settings.timeout)
            reactor.connectSSL(settings.server, settings.port, factory, ssl.ClientContextFactory())

    reactor.run()

if __name__ == '__main__':
    main()