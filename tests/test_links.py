from edscrape import links


def test_thread_url():
    assert links.thread_url(39026, 3626361) == "https://edstem.org/au/courses/39026/discussion/3626361"


def test_answer_url():
    assert (
        links.answer_url(39026, 3626361, 7875984)
        == "https://edstem.org/au/courses/39026/discussion/3626361?answer=7875984"
    )


def test_comment_url():
    assert (
        links.comment_url(39026, 3626361, 7876059)
        == "https://edstem.org/au/courses/39026/discussion/3626361?comment=7876059"
    )


def test_course_url():
    assert links.course_url(39026) == "https://edstem.org/au/courses/39026/discussion"
