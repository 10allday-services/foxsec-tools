#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
    Create a dot file showing table dependencies based on SQL files
    supplied on the commandline.

    Parsing uses heuristics to be "close enough". Tables which are
    internal only (e.g. created via the 'WITH' statement) are shown in
    blue. (Single connection table names are may also be internal, but
    not identified as such.)
"""

"""
    None of the SQL parsers I tried accepted our Athena/ReDash syntax.
    So I use the stdlib Python tokenizer to get tokens, and a small
    state machine to get a 95% fit.
"""

import argparse
import collections
import io
import sys
import tokenize

from loguru import logger

# our backup scripts create SQL files with the table name as the file
# name. Plus the ".sql" extension, of course. We'll need to strip that
# later.
sql_extension = ".sql"

# table names often directly follow these keywords
table_leadin = {"from", "join", "with"}

# tables following these keywords are internal to the query and rendered
# differently
internal_tables = {"with"}

# if any of these keywords are first after a table_leadin, they cancel
# the leadin. Example "... JOIN (SELECT a, b, c FROM real_table)"
state_reset = {"select"}

# Some queries are written with the database name preceding the table
# name.  We don't want that, so skip those names
skip_words = {"foxsec_metrics", "foxsec_metrics_stage"}

# Since we're abusing the Python tokenizer, we need to handle some cases
# ourselves. In particular, comments and bogus (for us) IndentationError
# exeptions.
def safe_get_next(generator):
    skip_to_newline = False
    last_was_hyphen = False
    while True:
        try:
            w_type, w, _, _, _ = next(generator)
            if w in ["#"]:
                skip_to_newline = True
            elif w in ["-"] and last_was_hyphen:
                skip_to_newline = True
            if not skip_to_newline:
                last_was_hyphen = bool(w == "-")
                yield w_type, w.lower()
            elif w_type == tokenize.NEWLINE:
                skip_to_newline = False
                last_was_hyphen = False
        except IndentationError:
            logger.info("Ignoring IndentationError")
            w_type = tokenize.OP
            w = "."
            yield w_type, w.lower()


def parse_tables(g):
    table_name_next = False
    internal_table_next = False
    table_names = []
    for w_type, w in safe_get_next(g):
        if w_type == tokenize.NEWLINE:
            continue
        if w_type == tokenize.OP:
            continue
        logger.debug("{} {} table {} internal {}", w_type, w,
                table_name_next, internal_table_next)
        if table_name_next and w_type == tokenize.NAME:
            # handle "... from (select ..."
            if w in state_reset:
                table_name_next = False
                internal_table_next = False
                logger.debug("{} table {}, internal {}", w,
                        table_name_next, internal_table_next)
            else:
                if w in skip_words:
                    # don't change anything
                    continue
                elif internal_table_next:
                    table_names.append("<{}>".format(w))
                    internal_table_next = False
                    logger.debug("{} table {}, internal {}", w,
                            table_name_next, internal_table_next)
                else:
                    table_names.append(w)
                    logger.debug("{} table {}, internal {}", w,
                            table_name_next, internal_table_next)
                table_name_next = False
                logger.debug("{} table {}, internal {}", w,
                        table_name_next, internal_table_next)
        elif w in table_leadin:
            table_name_next = True
            if w in internal_tables:
                internal_table_next = True
            logger.debug("{} table {}, internal {}", w,
                    table_name_next, internal_table_next)
    return table_names

group_colors = {
    "internal": "lightblue",
    "redash": "green",
    "views": "gold",
    "": "chocolate",
}

def output_group(name, members):
    color = group_colors.get(name, "pink")
    if members:
        print("""subgraph cluster{} {{
            node [color={}, style=filled];
            color={};
            rank = "same";
            "{}"
        }}""".format(name, color, color, '"; "'.join(members)))


@logger.catch
def process_files(file_list):
    global logger
    #logger.add(sys.stderr, format="{time} {extra[file_name]} - {message}")
    logger = logger.bind(file_name="<global>")
    internal_tables = set()
    table_groups = collections.defaultdict(set)
    edges = collections.defaultdict(set)
    for file_name in file_list:
        logger = logger.bind(file_name=file_name)

        with open(file_name, "rb") as f:
            table_names = parse_tables(tokenize.tokenize(f.readline))
        if file_name.endswith(sql_extension):
            table_name = file_name[: -len(sql_extension)]
        table_name = table_name[table_name.rindex('/')+1:]
        try:
            file_group = file_name[:file_name.rindex('/')].replace('/',
                    '').replace('.', '')
        except ValueError:
            file_group = ''
        table_groups[file_group].add(table_name)

        for t in table_names:
            if t.startswith("<"):
                # Prepend table name, so unique
                t = t[0] + table_name + "-" + t[1:]
                table_groups["internal"].add(t)
                # internal tables go both ways
                edges[table_name].add(t)

            edges[t].add(table_name)
    return table_groups, edges

def output_edge(tail, head):
    if tail.startswith("<"):
        print('"{}" [shape="box"];'.format(tail))
    print('"{}" -> "{}";'.format(tail, head))

def output_graph(groups, edges):
    global logger
    print(r"""strict digraph {
            rankdir = LR;""")
    logger = logger.bind(file_name="<global>")
    for k, v in groups.items():
        output_group(k, v)
    for tail, heads in edges.items():
        for head in heads:
            output_edge(tail, head)
    print(r"""}""")

def extract_tree(orig_groups, orig_edges, root_node):
    """
        Return subgraph rooted at root_node

        Naive implementation
    """
    nodes_to_find = {root_node}
    all_nodes = set()
    groups = collections.defaultdict(set)
    edges = collections.defaultdict(set)
    while nodes_to_find:
        all_nodes.update(nodes_to_find)
        next_nodes = set()
        for node in nodes_to_find:
            # still need to search all heads reachable from node
            downstream = orig_edges[node]
            next_nodes.update(downstream)
            edges[node].update(downstream)
        # don't search any we already have (internal tables are
        # circular)
        nodes_to_find = next_nodes - all_nodes
    for k, v in orig_groups.items():
        groups[k] = v & all_nodes
    return groups, edges

def flip_edges(orig_edges):
    edges = collections.defaultdict(set)
    for tail, heads in orig_edges.items():
        for head in heads:
            edges[head].add(tail)
    return edges

def extract_inverted_tree(orig_groups, orig_edges, end_node):
    flipped_edges = flip_edges(orig_edges)
    groups, reversed_edges = extract_tree(orig_groups, flipped_edges, end_node)
    return groups, flip_edges(reversed_edges)

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="Restrict to subgraph starting at SOURCE")
    parser.add_argument("--sink", help="Restrict to subgraph ending at SINK")
    parser.add_argument("files", nargs="+", help="SQL files to be parsed")
    return parser.parse_args()


@logger.catch
def main():
    args = parse_args()
    if args.files:
        groups, edges = process_files(args.files)
        if args.source:
            groups, edges = extract_tree(groups, edges,
                    args.source)
        elif args.sink:
            groups, edges = extract_inverted_tree(groups, edges,
                    args.sink)
        output_graph(groups, edges)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
