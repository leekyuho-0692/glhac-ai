"""Run directly with Python; no application startup or pytest hooks."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from sqlalchemy import Column, String, create_engine, or_
from sqlalchemy.orm import declarative_base, Session

Base = declarative_base()


class User(Base):
    __tablename__ = 'users'
    user_id = Column(String, primary_key=True)
    username = Column(String)
    display_name = Column(String)
    org_id = Column(String)
    role = Column(String)
    created_at = Column(String)
    last_login_at = Column(String)


class Org(Base):
    __tablename__ = 'orgs'
    org_id = Column(String, primary_key=True)
    name = Column(String)


class StaffSignupRequest(Base):
    __tablename__ = 'signups'
    request_id = Column(String, primary_key=True)
    username = Column(String)
    display_name = Column(String)
    status = Column(String)
    created_at = Column(String)


class ConsultantProfile(Base):
    __tablename__ = 'profiles'
    consultant_id = Column(String, primary_key=True)
    display_name = Column(String)


def load_functions():
    tree = ast.parse((Path(__file__).parents[1] / 'app/main.py').read_text())
    names = {'admin_list_users', '_user_display_name'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for n in nodes:
        n.decorator_list = []
        n.args.defaults = []
        for arg in n.args.args:
            arg.annotation = None
    ns = {'models': SimpleNamespace(User=User, Org=Org,
          StaffSignupRequest=StaffSignupRequest, ConsultantProfile=ConsultantProfile), 'or_': or_}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<actual search functions>', 'exec'), ns)
    ns['_user_base'] = lambda db, u, mask: {
        'user_id': u.user_id, 'display_name': ns['_user_display_name'](db, u)}
    return ns['admin_list_users']


class UserSearchTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        for i, name in enumerate([None, '임이순', None, None]):
            self.db.add(User(user_id=str(i), username='login'+str(i), display_name=name,
                             org_id='org'+str(i), role='consultant'))
        self.db.add_all([
            StaffSignupRequest(request_id='a', username='login0', display_name='임이순', status='approved'),
            StaffSignupRequest(request_id='b', username='login0', display_name='임이순', status='approved'),
            StaffSignupRequest(request_id='c', username='login3', display_name='임이순', status='pending'),
            ConsultantProfile(consultant_id='2', display_name='임이순')])
        self.db.commit()
        self.search = load_functions()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def query(self, **kw):
        args = dict(user={}, db=self.db, limit=500, offset=0, q='', sort='username',
                    dir='asc', meta=0, page=1, size=20, role='', status='', org_id='')
        args.update(kw)
        return self.search(**args)

    def test_names_across_orgs_and_existing_approved_accounts(self):
        result = self.query(q=' 임이순 ', org_id='unrelated')
        self.assertEqual(result['total'], 3)
        self.assertEqual({r['user_id'] for r in result['items']}, {'0', '1', '2'})
        self.assertTrue(all(r['display_name'] == '임이순' for r in result['items']))

    def test_pagination_does_not_duplicate_signup_matches(self):
        result = self.query(q='임이순', size=2, page=2)
        self.assertEqual((result['total'], result['pages'], len(result['items'])), (3, 2, 1))

    def test_org_filter_without_search_and_role_filter(self):
        self.assertEqual(self.query(org_id='org1')['total'], 1)
        self.assertEqual(self.query(q='임이순', role='auditor')['total'], 0)

    def test_literal_wildcards_and_login_search(self):
        self.assertEqual(self.query(q='%')['total'], 0)
        self.assertEqual(self.query(q='login3')['total'], 1)


if __name__ == '__main__':
    unittest.main()
