from app.services.keywords import document_vector, query_vector, token_id, tokenize


def test_tokenize_keeps_numbers_like_3_2_and_cpp_and_identifiers():
    assert tokenize("Theorem 3.2: Split-brain in C++ and get_view().") == [
        "theorem",
        "3.2",
        "split",
        "brain",
        "in",
        "c++",
        "and",
        "get_view",
    ]


def test_tokenize_ignores_sentence_punctuation():
    assert tokenize("The primary. The backup!") == ["the", "primary", "the", "backup"]


def test_token_ids_are_stable_32_bit_numbers():
    assert token_id("heap") == token_id("heap")
    assert token_id("heap") != token_id("heaps")
    assert 0 <= token_id("anything") < 2**32


def weights(vector) -> dict[int, float]:
    return dict(zip(vector.indices, vector.values))


def test_document_vector_counts_words_with_diminishing_returns():
    vector = weights(document_vector("brain brain brain heap"))
    brain, heap = vector[token_id("brain")], vector[token_id("heap")]
    assert brain > heap  # repeated words weigh more...
    assert brain < 3 * heap  # ...but three mentions aren't worth three times one


def test_long_chunks_dont_win_just_by_being_long():
    short = weights(document_vector("paxos consensus"))[token_id("paxos")]
    long = weights(document_vector("paxos " + "filler " * 300))[token_id("paxos")]
    assert short > long


def test_vectors_are_sorted_and_unique():
    vector = document_vector("b a c a b")
    assert vector.indices == sorted(set(vector.indices))
    assert len(vector.indices) == 3


def test_empty_text_gives_an_empty_vector():
    assert document_vector("").indices == []
    assert query_vector("?!").indices == []


def test_query_vector_counts_each_word_once():
    vector = query_vector("view service view")
    assert sorted(vector.indices) == sorted({token_id("view"), token_id("service")})
    assert set(vector.values) == {1.0}
